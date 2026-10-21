# Design Asset Readiness Lab

The Design Library now connects acquired design evidence, adopted model revisions,
executable Reference trials and scoped measurement agreement. Open
`http://127.0.0.1:8780/design-library.html?family=sensor&asset=sensor-e71903206aefdc62550da6f5`.

## Demonstration

1. Inspect the whole-library counts, then the selected asset's source and applied
   values. Missing values are unknown; model defaults and user assumptions have
   separate labels. Source completeness is not a physical accuracy percentage.
2. Inspect IMX462's legacy extraction and reviewed evidence. The reviewed DTI
   description is not a simulated DTI effect. The CFA thickness assumption is
   already used in the Reference model; reviewing it does not change its RAW.
3. Save an explicit read-noise or dark-current proposal with a reason. Adopt it
   as a new immutable revision, run its preparation check, then run a Reference
   trial. The source database, previous revision and original RAW remain intact.
4. Open the completed trial against its exact baseline. Compare uniform-field
   temporal noise, texture, color and SFR at center and outer-ne. The report and
   package retain the original response hashes and applied electrical model.
5. Use **변경 확인** to inspect registered local sources. Optionally enable the
   15-minute server-local watch. Watch starts OFF, detects changes and creates
   proposals; it never adopts a revision or launches a simulation automatically.

The first execution adapter supports the exact reviewed Reference IMX462 source
and only `read_noise_e` and `dark_current_e_per_s`. Other design assets can retain
evidence and proposed revisions, with missing adapter inputs explained. The
library does not imply arbitrary patent prescriptions or sensor reports are
ready to produce RAW.

## Status and publication rules

- **Evidence:** source facts, derivations, defaults, user assumptions and unknown
  fields are retained independently. Extracted values can disagree with reviewed
  evidence; the applied value and supported effect are shown separately.
- **Execution:** a check applies to a particular adopted revision and supported
  Reference profile. Changed source hashes make it stale. A check is not a run;
  only a completed trial supplies new RAW/ISP evidence.
- **Agreement:** only an exact revision/run/measurement/test binding can support
  a named metric and condition. No measurements are supplied by this feature;
  current models remain `not_established` for product agreement.

Proposals, adoption records, source snapshots, checks and validation links live
in the platform Registry. They never overwrite the lens or sensor source DB.
Unavailable sources retain previous observations as history. A cancelled check
cannot become the model's current successful preparation check.

The watch observes registered local catalog and Reference-input files. It does
not crawl vendors or download reports. It runs while this server is active,
coalesces concurrent refresh requests and does not replay missed intervals.

## Real recapture contract

Each adopted-readout trial uses preserved **pre-noise photoelectron rates** from
the original acquisition at center and outer-ne. Four charts, native 256×256
RGGB and eight independent repeats produce **64 new integer RAW files** per
trial. It uses the same source seeds for paired comparisons, reproduces the
baseline RAW exactly before comparing, then applies the changed electrical
model. No captured RAW is rescaled to imitate new noise or dark current.

Lens/pixel response, source exposure/gain, ISP, CCM and native LSC stay fixed.
Temporal noise is the square root of the spatial mean of per-pixel temporal
sample variance across eight repeats. Spatial texture is not counted as noise.

The SSD project keeps the existing 500 GB decimal budget and 5 GiB volume
reserve. Trials reserve 512 MiB and check actual storage while processing. If
the source, capacity or exact baseline reproduction fails, the trial fails;
it does not silently reduce its resolution or substitute a model.

## API and SDK

All new routes are under `/api/v1/component-explorer`:

| Route | Meaning |
| --- | --- |
| `GET /readiness` | Whole-library independent status counts |
| `GET /assets/{id}/readiness` | Parameters, reconciliation, models and history |
| `POST /refreshes` with `{}` | Queue registered-local-source inspection |
| `GET/PUT /watch` | Read or set `{ "enabled": true/false }`; fixed 900 seconds |
| `POST /proposals` | Pin explicit changes and reason |
| `POST /proposals/{id}/adopt` | Explicit adoption; repeated request is idempotent |
| `POST /readiness-checks` | Queue a check for `model_revision_id` |
| `POST /model-trials` | Acquire from an exact `source_run_id` and adopted revision |
| `GET /jobs/{id}` | Typed job progress and result |
| `POST /jobs/{id}/cancel` | Common cancellation lifecycle |
| `POST /validation-links` | Bind `model_id` and existing `validation_id` |

The common SDK `wait()` works for refresh, check and trial jobs. A timeout keeps
the job ID usable for resumed polling. Example:

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=180) as client:
    evidence = client.design_readiness("sensor-e71903206aefdc62550da6f5")
    proposal = client.propose_design_asset_update({
        "asset_id": evidence["asset"]["id"],
        "name": "Read-noise sensitivity; assumed value",
        "source_run_id": "run-d2105e38b3cd4f83b3550ebff21ca7fd",
        "changes": [{"field": "read_noise_e", "value": 1.0,
                     "origin_kind": "user_assumption",
                     "reason": "Sensitivity study, not product measurement"}],
    })
    model = client.adopt_design_asset_proposal(proposal["id"])
    check = client.submit_design_readiness_check(model["id"])
    checked = client.wait(check["id"], timeout=360)
    assert checked["status"] == "succeeded"
    assert checked["result"]["report"]["execution_ready"]
    trial = client.submit_design_asset_trial({
        "model_revision_id": model["id"],
        "source_run_id": model["source_binding"]["run_id"],
    })
    result = client.wait(trial["id"], timeout=1900)
    assert result["status"] == "succeeded"
    print(result["result"]["summary"]["result_url"])
```

`tools/run_design_asset_readiness_demo.py` prepares and verifies the 0.5× and 2×
read-noise assumption demos through the same API. Receipts are written to
`outputs/design-readiness-delivery-20261012/`. It verifies package hashes and
unchanged parent RAW and source DB, and records measured time/storage costs.

## Prepared delivery — 2026-10-12

The live library shows 1,695 catalog assets, two adopted readout revisions and
two completed Reference trials. Product measurement agreement is unestablished.
The IMX462 input table contains 21 observations from its selected catalog
revision plus explicitly mapped current inputs and assumptions (44 total rows).

| Read-noise assumption | Center temporal noise | Outer-ne temporal noise | RAW |
| --- | --- | --- | --- |
| Baseline 10 e⁻ RMS | 26.730 e⁻ | 22.707 e⁻ | Preserved acquisition |
| 5 e⁻ RMS | 25.283 e⁻ (−5.41%) | 20.983 e⁻ (−7.59%) | 64 new captures |
| 20 e⁻ RMS | 31.856 e⁻ (+19.18%) | 28.558 e⁻ (+25.77%) | 64 new captures |

These are total temporal noise values on the uniform chart under the fixed
source conditions, not read-noise-only measurements or product claims.

- [5 e⁻ assumption against baseline](http://127.0.0.1:8780/reference-workshop.html?run=run-e8bb4425cfe449c7929419a812aefae7&baseline=run-d2105e38b3cd4f83b3550ebff21ca7fd&field=outer-ne&chart=uniform)
- [20 e⁻ assumption against baseline](http://127.0.0.1:8780/reference-workshop.html?run=run-1f18f857c80e4b418aa03ab43d00763b&baseline=run-d2105e38b3cd4f83b3550ebff21ca7fd&field=outer-ne&chart=uniform)

Each package has 309 hashed files. Combined logical storage is 292,947,058 bytes;
SSD allocation is 363,331,584 bytes. The API-driven preparation and full artifact
verification took 540.7 seconds, including the checks and two sequential trials.
Readiness-detail loading measured 13.16 seconds initially and 3.22 seconds warm
on this host; metadata readiness does not run the optical solver.

The focused Python suite passed 64 tests. Readiness, Reference bindings, ISP Lab
and ISP tuning browser fixtures passed. Live browser testing exercised watch
enable/disable, queued refresh cancellation and completed trial opening. Manifest,
native RAW and report HTTP endpoints were checked against their saved hashes.
The original catalog and sensor JSON hashes and all parent package hashes stayed
unchanged. Source-change and late-cancellation edge cases use isolated fixtures;
no source DB was altered to manufacture a live update.

Watch was returned to OFF. Re-enabling it in the UI starts registered-local-source
inspection while the server is running. Readout trials retain the source's RAW
replay path; a fresh general Reference capture cannot silently discard an
adopted readout revision and is disabled in that view.
