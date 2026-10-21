# Design Asset Explorer

`/design-library.html` connects the existing lens and sensor evidence catalog to
source structures, engineering shortlists and retained simulation results. It
runs on the existing 8780 server and does not alter or rebuild source databases.

## Five-minute demonstration

1. Open the lens map. Explain that components, prescriptions/configurations and
   results are different counts. Each plotted point is a specific design asset.
2. Use **F/2 이하 렌즈** and select a point. Inspect focal length, aperture,
   surfaces/materials and missing source information. Add up to four lenses to
   the comparison. No automatic product performance ranking is produced.
3. Switch to sensors and use **2µm 이하 센서**. Compare pitch and optical stack
   height. Separate report records with the same model name remain separate.
   Missing axis values stay visible in the list rather than becoming zeros.
4. Use **계산 결과가 연결된 자산**. Open a retained result from the selected asset.
   The server verifies its source/manifest binding before returning a workbench
   link. A reviewed derivative is identified as such, not as execution of an
   older extracted model. Source membership alone is not physical validation.
5. Save a shortlist with a design question and candidate reasons. Reopen it and
   download the pinned JSON. A changed/missing source is marked stale/missing;
   the saved candidate is never silently replaced by a newer revision.

## Data and geometry meaning

The adapter reads `camerae2e_db/catalog.sqlite` and the local sensor report
catalog. Maps use metadata only; PSF and RAW arrays are not loaded to browse the
library. A result badge means stored, source-linked evidence is available, not
that a new simulation has run or every source artifact was just reverified.

Lens curvature and axial spacing come from the recorded prescription. Unknown
aperture radius/diameter conventions and unsupported aspheric shape must not be
turned into an exact recovered lens outline. Sensor dimension bars depict the
recorded dimensions, not an inferred layer order. DTI, OCL or circuit information
in a report does not by itself imply a corresponding solver model exists.

The external-demo view excludes original PDFs, proprietary source images, full
report bodies and absolute source paths. Source identities and derived geometry
do not grant new redistribution rights. Source facts, generated-model assumptions
and conflicting extraction/review evidence remain distinct.

## Public API and SDK

All routes live under `/api/v1/component-explorer`:

| Route | Behavior |
| --- | --- |
| `GET /catalog` | Metadata, separate counts, facets, optional filtering |
| `GET /assets/{id}` | Source-pinned details, structures, evidence and result links |
| `GET /assets/{id}/results/{result_id}` | Verify the exact result and return its existing workbench URL |
| `POST /shortlists` | Save one to four same-family candidates with exact revision/content hashes |
| `GET /shortlists` | List saved shortlists |
| `GET /shortlists/{id}` | Retrieve unchanged saved data plus current/missing/stale status |

Search supports `family`, `q`, `manufacturer`, `cfa_pattern`, `pixel_architecture`,
`has_results`, `focal_length_min/max`, `f_number_min/max`, `pixel_pitch_min/max`,
`stack_height_min/max`, `limit` and `offset`. Numeric filters exclude missing
values. No filter imputes a measurement. Units are mm, dimensionless F/# and µm.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=180) as client:
    catalog = client.search_design_assets(family="lens", f_number_max=2)
    candidates = catalog["items"][:3]  # Review the evidence before selecting.
    details = [client.design_asset(item["id"]) for item in candidates]
    saved = client.save_design_shortlist({
        "name": "F/2 design investigation",
        "question": "Which source prescriptions should receive further engineering review?",
        "items": [{
            "asset_id": item["id"], "revision_id": item["revision_id"],
            "content_sha256": item["content_sha256"],
            "reason": "Meets the declared aperture filter; performance not yet ranked",
        } for item in candidates],
    })
    restored = client.design_shortlist(saved["id"])
```

This release adds discovery and evidence reuse. It does not add an arbitrary
DB-to-RAW adapter, a solver campaign, a new physical model or product ranking.

The [Design Asset Readiness Lab](camera-platform-design-readiness.md) extends
this view with source-change proposals, explicit adoption, per-revision checks
and bounded Reference readout trials. Its check, execution and measured-agreement
states remain independent.

## Prepared delivery — 2026-10-12

- [Explorer](http://127.0.0.1:8780/design-library.html)
- [Sensor structure comparison](http://127.0.0.1:8780/design-library.html?family=sensor&asset=sensor-e71903206aefdc62550da6f5&shortlist=component_explorer_shortlist-d1773f6a346d478bb062f6d3bed0567f): IMX462, IMX415 and IMX678 report revisions. Pitch, shared-pixel architecture and reported dimensions; no performance ranking.
- [Patent lens comparison](http://127.0.0.1:8780/design-library.html?family=lens&asset=lens-4de1b561bc4f3c23c08a27fb&shortlist=component_explorer_shortlist-174be737654d422d8eca10428ff9357a): three exact US10310255B2 prescriptions labeled wide-angle, intermediate and telephoto. They may come from different embodiments; this is not a recovered continuous zoom mechanism.

The live catalog contains 877 lens components, 1,471 lens revision/configuration
entries and 224 sensor report entries (1,695 explorer items). F/2 filtering
returns 195 lens entries; pitch at most 2 µm returns 98 sensor entries. Missing
coordinates remain in the unfiltered list.

Only two source assets currently bind to retained results: the exact Reference
patent-lens revision and the IMX462 source report. Each has 41 links to the same
retained study lineage. These are not 82 independent camera products. The sensor
links explicitly identify reviewed derivatives of the source report rather than
claiming execution of the older extracted revision.

Measured initial HTTP catalog loading was 13.43 s (including concurrent browser
startup); subsequent filtered requests were 59–64 ms. Metadata browsing loads no
RAW arrays. The initial scan may repeat after Registry changes. Full source and
artifact verification runs when opening a result; the actual IRCF result passed
this check and opened with its preserved RAW/ISP comparison.

Saving and reopening both comparison lists, JSON download, real source-backed
details, the small-viewport filter panel and IRCF result navigation were checked
in the browser. The focused Python suite passed 39 tests, the UI fixture passed,
and the new Python modules passed Ruff. Original catalog and sensor JSON SHA-256
values were unchanged. Receipts and screenshots are in
`outputs/design-library-delivery-20261012/`.

Saved shortlist content is immutable. Retrieval adds current source status and
`current_result_references`; it keeps historical `result_references` separately.
`results_verified: false` means current links still require verification on open.
If source storage is unavailable, the saved snapshot remains readable with
`source_unavailable`; new catalog requests return 503 without disclosing paths.
