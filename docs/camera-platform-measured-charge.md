# Preserved nonlinear RAW and response calibration — dev12

Dev12 implements imported nonlinear acquisitions, separately selected response calibrations, and their connection to charge-domain ISP v3 and Study v2. The frozen implementation has passed the platform regression, scoped rendered-browser and actual HTTP workflows, restart readback, and exact backup/restore checks described below. This slice preserves the platform's broader optical, pixel, electrical, HDR/LFM and ISP scope; it does not establish physical-camera accuracy or product release readiness.

## Acquisition and calibration are separate records

The `captures` collection accepts `camera_nonlinear_capture_v1`. It preserves the original `raw-capture.npy` and `acquisition-metadata.json` bytes, including uploaded metadata formatting. Import does not create a linear ROI measurement, synthetic scene or capture program, and does not select a response calibration.

Sequential acquisitions retain `[F,H,W]`; paired DCG acquisitions retain `[A,2,H,W]` with the physical HCG/LCG order. Metadata binds the assembly, Test and sensor-model snapshots, native crop and CFA, pixel pitch, RAW formats, dataset/group identities, instrument and ordered frames. The original container, acquisition and path/frame pixel identities are retained. Rewrapping identical pixels or selecting the other DCG path cannot manufacture independent exposures.

Each frame distinguishes `recorded_applied`, `command_only` and `unknown` settings. Exposure, analog/digital gains and temperature are required for calibrated replay; commands do not become applied values. Missing clock, shutter or timestamps stay explicitly unknown. Known timing produces exposure-window metadata from recorded settings, not a simulated electronic schedule or a claim of timestamp accuracy.

The `readout_calibrations` collection retains `camera_nonlinear_response_calibration_v1`. Its normalized electrical model is either nonlinear single-path v2 or paired DCG v2, including the paired read-noise covariance declaration. Native sensor/geometry/pitch and physical-instance bindings, applicability, instrument, dataset role (`development` or `holdout`), dataset IDs, reference captures and parent calibrations are frozen with the record.

Each response preserves three original artifacts:

| Artifact | Meaning |
| --- | --- |
| `readout-calibration-metadata.json` | Original calibration declaration and provenance |
| `expected-dark-rate.npy` | Finite nonnegative `[H,W]` expected dark rate in electron/pixel/s at the model reference temperature |
| `readout-evidence.json` | `camera_nonlinear_response_evidence_v1`, with identified documents and explicitly named/unit-bearing numeric table columns |

The dark-rate map is cropped to the capture's native coordinates and scaled by the declared temperature law and each recorded exposure. It is neither dark RAW DN nor a stochastic dark realization. Imported calibration disallows synthesized random PRNU/DSNU. All settings and domain limits are checked before allocating the frame-by-pixel dark maps.

An absolute electron scale requires an identified `external_charge`, `external_radiometric`, `vendor_calibration` or `authored_simulation` reference. The declaration includes source, method/version, instrument, linked original evidence and a nonnegative absolute/relative bound or standard uncertainty with matching units. A measured capture cannot use an authored-simulation anchor and requires an exact physical-instance binding. Supplied evidence is not independently authenticated, and an anchor's uncertainty is not a complete uncertainty budget: total calibration uncertainty remains explicitly unknown. This import path does not fit a response curve or infer an absolute charge scale from unanchored RAW.

Unanchored or unknown-settings acquisitions can therefore be preserved and inspected without entering calibrated replay. Capture import accepts four Bayer CFA declarations; the current nonlinear response/ISP path supports its existing native RGGB model and rejects unsupported format bindings.

## Validation and publication

Original metadata JSON is parsed strictly, including duplicate-key and nonfinite-value refusal. NPY headers, exact payload length, integer RAW codes, shape, order and pixel budgets are checked before array loading. RAW import is limited to 64 MiB, at most 64 acquisitions, even native dimensions no larger than 4096, and 1,048,576 selected-path pixels. Calibration preparation has a separate 1,000,000 frame-pixel limit.

The three response artifacts total at most 64 MiB. HTTP additionally limits original metadata to 1 MiB and response evidence JSON to 2 MiB; oversized evidence is rejected with HTTP 413 rather than retaining a truncated prefix. Response evidence allows bounded rectangular numeric tables, up to one million cells. The contract modules remain authoritative for all detailed limits.

Capture duplicate identity checks and publication share a registry transaction. Response publication compares the complete referenced record read-set atomically, so a changed sensor, parent calibration or reference capture cannot be published from stale validation. Subsequent reads reverify original bytes and frozen reference closure. The new response-calibration artifact owner follows existing owner priorities; historical records and cache keys are not migrated.

## Replay, Study and evidence closure

`camera_nonlinear_raw_selection_v2` supplies `readout_calibration_id`, plus `mode: sequential_frames` or `mode: shared_charge_path` and `readout_id: hcg|lcg`. Selecting another response creates a new interpretation of the same preserved acquisition. It does not reimport the RAW or give it a new exposure identity. Original run-only selection v1 remains supported with its previous source dictionaries and cache bindings.

The capture adapter verifies sensor, format, native crop, physical instance and recorded exposure/temperature/gain applicability, then prepares the same nonlinear calibration inputs used by ISP v3. Sequential analog gain must equal the recorded common gain. Paired DCG uses its calibrated fixed path gains and requires common gain 1. DCG replay selects one physical readout while retaining the paired acquisition identity; it does not flatten both paths into independent frames or perform a new joint DCG fusion. Normalization uses the declared shared full-well charge, never a path's effective saturation ceiling.

ISP v3 retains calibrated signed photocharge, local source-noise variance, quantization bounds and validity masks separately. LSC, local charge-variance denoise, demosaic, white balance, CCM, linear sharpen and tone use the existing versioned graph. Invalid physical values remain masked; display black is not a physical zero estimate. Output covariance and a complete measurement uncertainty budget are not claimed. The numerical inverse and clipping limits remain those documented in [nonlinear readout](camera-platform-nonlinear-readout.md) and [nonlinear ISP](camera-platform-nonlinear-isp.md).

Study v2 can use preserved captures and explicit response choices for bounded candidate tuning and selected-candidate validation. Repeat-noise reduction requires fixed recorded settings and an explicit stationary-scene declaration. Overlap checks use typed exposure identities across captures, datasets, acquisition groups, original/path/frame pixels, parent acquisitions and decoder-fitting reference ancestry. Shared response or dark-map bytes alone do not make different exposures overlap. Declared disjoint data is not proof of physical independence or authenticated provenance.

Replay and Study exports retain response parents, original response evidence, reference-capture ancestry, and associated model/Factory ancestry. This includes an alternate same-sensor lens/Factory used by a fitting capture's parent. Direct response `reference_capture_ids` currently require nonlinear captures; legacy capture ancestry is supported transitively.

The API provides `GET /api/v1/templates/nonlinear-capture`, `GET /api/v1/templates/readout-calibration`, `POST /api/v1/captures/nonlinear/import`, and `POST /api/v1/readout-calibrations/import`. The SDK supplies `import_nonlinear_capture(metadata_path, raw_path)` and `import_readout_calibration(metadata_path, dark_rate_path, evidence_path)`. Workbench upload, response/path selection and replay authoring use the same records and checks.

## Verification and remaining limits

Evidence is under [`reports/camera_platform/measured-charge-v016`](../reports/camera_platform/measured-charge-v016). The frozen Python environment digest is `9df43b4cd964f2685409a4519d1a8228a9dbc8c20b3e474f9db6f1d9ce70460b`. Results recorded on 2026-09-22:

| Completed scope | Result | Evidence |
| --- | --- | --- |
| Full `tests/platform` regression | 3,463 passed, 0 failed; 835.95 s | [log](../reports/camera_platform/measured-charge-v016/platform-tests.log), [JUnit](../reports/camera_platform/measured-charge-v016/platform-tests.xml) |
| Corrected focused API/integration suite | 40 passed | [JUnit](../reports/camera_platform/measured-charge-v016/api-corrected.xml) |
| Capture Study integration / Study v2 regression | 20 / 67 passed | [capture Study](../reports/camera_platform/measured-charge-v016/study-regression.xml), [Study v2](../reports/camera_platform/measured-charge-v016/study-v2-regression.xml) |
| Independent review regressions | 2 passed: oversized evidence refusal and alternate fitting-Factory export closure | [review amendment](../reports/camera_platform/measured-charge-v016/independent-integration-review-amendment.json) |
| Browser module/event-handler tests | 128 passed, using API/DOM stubs | [summary](../reports/camera_platform/measured-charge-v016/browser-tests-final-v2.json) |
| Actual rendered-browser workflow | Original response/RAW uploads, profile registration, successful Worker replay, frame navigation/reload and correctly bound Study draft | [browser journal](../reports/camera_platform/measured-charge-v016/browser-verification.json) |
| Actual HTTP workflow | 1,013 checks passed: sequential/HCG/LCG and alternate-response replay, tuning/selected-candidate validation, 34 pinned record/export/analysis snapshots, 226 artifact identities, 6 bundles | [successful journal](../reports/camera_platform/measured-charge-v016/http-resumed/http-verification.json) |
| API/Worker restart and HTTP readback | Separate 1,519-check GET-only pass; saved evidence, arrays and bundle member bytes retained | [process proof](../reports/camera_platform/measured-charge-v016/restart-process-verification.json), [readback](../reports/camera_platform/measured-charge-v016/http-restart-readback.json) |
| Browser-authored records after restart | 4 saved records and their 47 artifacts retained byte-for-byte | [saved readback](../reports/camera_platform/measured-charge-v016/browser-readback-restarted.json) |
| New dev12 QA backup/restore | 73 records, 29 succeeded jobs, 160 audit rows, 2,704 owner rows, 727 binding heads and all 1,268 blobs exact; original source rows unchanged | [backup proof](../reports/camera_platform/measured-charge-v016/backup-restore-verification.json), [source/response binding](../reports/camera_platform/measured-charge-v016/backup-source-binding.json) |
| Saved dev11 evidence readback on dev12 | 15 HTTP records, 184 artifacts, 6 bundles / 1,321 exact member-byte matches, 2 analyses | [GET-only proof](../reports/camera_platform/measured-charge-v016/legacy-readback/summary.json) |

Focused test counts overlap the full regression; they are not additive. The full suite includes 35 nonlinear-capture boundary tests and 44 response-calibration tests. The dev11 readback also verified that all 53 baseline records, 18 jobs, 107 audit rows, 1,566 owner rows, 474 binding heads and 900 original blobs were unchanged before and after GETs. It preserves saved schemas, units and cache bindings; it is not an old-version numerical rerun or a separate dev9/v1 state qualification.

The initial HTTP verifier incorrectly rejected the replay endpoint's successful 202 response. Its [original failed journal](../reports/camera_platform/measured-charge-v016/http-final/http-verification.json) is preserved. The successful run reused seven exact known successful responses without resending those POSTs. Restart readback is a separate report and leaves that successful journal unchanged. Check-label totals include correspondence checks and are not counts of independent numerical experiments.

The backup includes five imported response calibrations and four imported acquisitions. All 15 original response artifact descriptors (eight unique blobs) were independently compared between source and restored state. The 1,268 reachable blobs comprise 541 private graph objects and 727 public artifacts. Restore started no Worker and rewrote no logical rows.

The browser pass covers the recorded workflow and Study draft, not a complete Study executed through the browser. Authored synthetic files exercise import, calculation and evidence contracts; HTTP numerical comparisons use the same kernel. No measured hardware dataset, authenticated calibration, physical accuracy, complete uncertainty budget or customer release acceptance is established by these results.

Implementation boundaries: [`nonlinear_capture_contracts.py`](../src/camera_platform/nonlinear_capture_contracts.py), [`nonlinear_captures.py`](../src/camera_platform/nonlinear_captures.py), [`readout_calibration_contracts.py`](../src/camera_platform/readout_calibration_contracts.py), [`readout_calibrations.py`](../src/camera_platform/readout_calibrations.py), [`isp_capture_source.py`](../src/camera_platform/isp_capture_source.py).
