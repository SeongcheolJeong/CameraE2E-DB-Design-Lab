# Lens Patent Simulation DB

Generated from Lens_Patent_DB `expanded_v6` exports.

Migration update (2026-09-06): prefer `raytrace_psf_v2/`. This bundled v6 DB has
**1 validated geometric-preview configuration** (`p0014:intermediate`); the v9
package has **4**. These counts describe the current optical-data contract,
not calibrated camera image quality. Source identity, axes and array validity
are checked; diffraction, sampling convergence and absolute radiometry remain
unvalidated. `recommended_for_camerae2e` has the explicit scope
`geometric_preview_not_calibrated_IQ` where that quality table exists.

Files:

- `lens_patent_simulation_v6.sqlite`: prescription and simulation result catalog.
- `summary.json`: generation summary and status counts.
- `companies/`: company-specific SQLite DB subsets and manifest.
- `raytrace_psf_v2/`: current contract-v2 geometric PSF grid and manifest.
- `raytrace_psf/`: historical low-resolution geometric PSF grids.
- `raytrace_psf_highres/`: historical 512 x 512 preset sample; resolution is not validation.

Regenerate:

```bash
python tools/build_lens_patent_simulation_db.py
python tools/export_lens_patent_company_sets.py --overwrite
```

Use from the CameraE2E repository root:

```python
from pathlib import Path
from pyisetcam import lens_patent_search, lens_patent_raytrace_optics

root = Path("src/pyisetcam/data/lens_patents")
db = root / "lens_patent_simulation_v6.sqlite"
row = lens_patent_search(db_path=db, require_raytrace=True, limit=1)[0]
optics = lens_patent_raytrace_optics(
    row["simulation_id"], db_path=db, psf_dir=root / "raytrace_psf_v2",
    allow_approximate_radiometry=True,
)
```

Company-specific DB:

```python
from pyisetcam.lens_patents import lens_patent_company_db_path, lens_patent_search

canon_db = lens_patent_company_db_path('Canon')
canon_rows = lens_patent_search(db_path=canon_db, require_camerae2e=True)
```

Raytrace PSF grids:

```bash
/Users/seongcheoljeong/RayOptics/rayoptics-env/bin/python3.12 \
  tools/build_lens_patent_raytrace_psf_grid.py \
  --camerae2e-db src/pyisetcam/data/lens_patents/lens_patent_simulation_v6.sqlite \
  --out-dir src/pyisetcam/data/lens_patents/raytrace_psf_v2 \
  --preset production --simulation-id p0014:intermediate \
  --overwrite
```

Generated grids are compressed `.npz` files with a `manifest.json`. Pass the
matching `db_path` to keep source-prescription validation tied to this DB.
Downsampling preserves probability mass, but does not establish sensor sampling
accuracy:

```python
optics = lens_patent_raytrace_optics(
    "p0014:intermediate",
    db_path=db,
    psf_dir=root / "raytrace_psf_v2",
    target_psf_size=128,
    allow_approximate_radiometry=True,
)
```

The loader rejects old unversioned assets by default. Use `allow_legacy=True`
only for deliberate historical reproduction, with an explicit historical
`psf_dir`. Current v2 assets require the separate
`allow_approximate_radiometry=True` flag because relative illumination and
transmission are uncalibrated placeholders. Neither flag certifies image quality.

After regeneration, `tools/validate_lens_raytrace_assets.py --db <db> --output
<report.json> --write-db` refreshes the asset validation table and available
quality recommendations. The manifest includes failed attempts as well as
generated assets; file presence and old catalog status alone are not acceptance.

Main API:

- `lens_patent_db_summary()`
- `lens_patent_companies()`
- `lens_patent_company_sets_manifest()`
- `lens_patent_company_db_path(company)`
- `lens_patent_search(..., db_path=None, require_camerae2e=False, require_raytrace=False)`
- `lens_patent_get(simulation_id)`
- `lens_patent_surfaces(lens_id, configuration=None)`
- `lens_patent_optics(simulation_id, default_f_number=None)`
- `lens_patent_raytrace_psf_search(company=None, status=None)`
- `lens_patent_raytrace_optics(..., db_path=None, allow_legacy=False, allow_approximate_radiometry=False)`
- `lens_patent_downsample_psf(psf_function, target_psf_size)`

Caveat: these are patent-disclosed examples, not confirmed production lenses. Rows marked `paraxial_proxy` or with `proxy:` sources should be verified before being treated as ray-trace-accurate designs. The PSF grids are RayOptics geometric ray histograms, not diffraction or wave-optics PSFs. This DB is a CameraE2E optics/proxy catalog plus generated raytrace assets, not a full Zemax/OpticStudio parity result set.

Historical catalog summary (these are not current validated-asset counts): `{"camerae2e_ready_by_company": {"Canon": 144, "Konica Minolta": 24, "Largan": 67, "Nikon": 79, "Olympus/OM System": 57, "Panasonic": 138, "Samsung": 26, "Seiko Epson": 12}, "companies": 14, "lenses": 414, "readiness_counts": {"needs_variable_distances": 47, "ready_configured": 168, "ready_staging": 199}, "simulation_results": 793, "status_counts": {"camerae2e_ready": 547, "metadata_only": 4, "partial": 242}, "surfaces": 20693}`
