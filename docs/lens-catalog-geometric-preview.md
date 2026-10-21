# Lens DB revision: geometric preview boundary

`camera_platform.lens_catalog_bridge` reads an explicitly selected, immutable
component-catalog lens revision and its existing RayOptics v2 PSF. It checks the
catalog revision hash, source SQLite hash, v2 contract result, NPZ hash, and
artifact link before calculating center and edge geometric spot statistics.
It never writes to either catalog or the source DB.

```python
from camera_platform.lens_catalog_bridge import (
    compare_geometric_lenses, lens_revision_catalog,
)

choices = lens_revision_catalog(query="p0014")
for row in choices["revisions"]:
    print(row["revision_id"], row["simulation_id"],
          row["preview_status"], row["reason_code"])

result = compare_geometric_lenses([
    "6c48e967-74b4-5901-8186-ba6fafe4797b",  # p0014:intermediate
    "fd959d9d-8503-5fe4-a582-4cc9074e6bfd",  # p0019:intermediate
], wavelength_nm=550.0)
for lens in result["lenses"]:
    print(lens["identity"]["simulation_id"], lens["fields"])
```

The output gives the geometric ray-histogram EE80 and RMS radius in µm around
each PSF's intensity centroid, geometric chief-ray angle in degrees, and the
stored ideal image height in mm. It also reports the catalog revision, source
DB, source prescription, validation record, and PSF artifact hashes. The two
example revisions have different field limits (13.66 and 13.73 mm), focal
lengths, and f-numbers. This is a descriptive spot comparison at the same
stored wavelength and object distance, not a matched product ranking.

`lens_revision_catalog()` lists revisions by name, manufacturer, configuration,
source simulation ID, and preview status without loading PSF arrays. A ready
item has its source, prescription, and artifact hashes plus the manifest's
asset identity checked at listing time; the preview loader checks the full LUT
again. Search accepts a
name, publication, simulation ID, or revision ID. On the current catalog, 4
of 1,471 lens revisions are ready for this geometric preview. Other entries
include a reason code such as `validated_psf_missing` or
`unsupported_response_contract`.

The four current v2 assets have a valid geometric contract. Their radiometry
is `unknown`, sensor sampling is `unvalidated`, and diffraction is absent.
The loader's `allow_approximate_radiometry=True` is used solely to inspect the
geometric PSF; its unity RI/transmission placeholders are never returned as
usable lens response. The result therefore marks R1 RAW eligibility `blocked`.
If a selected revision has no v2 asset, a changed source/asset hash, or no
exact stored wavelength, the bridge fails with a specific error code. It does
not fall back to a diffraction-limited or legacy unvalidated PSF.

The catalog also contains 649 `public_optical_prescription` revisions and
geometric raytrace diagnostics linked to 30 of them. Those diagnostics are a
different artifact contract from the validated v2 PSF grid used here. Their
surface, material, field, and sampling semantics need a separate adapter and
checks before they can join this comparison.

To promote a lens into an R1 camera experiment, a separate adapter must supply
and check the absolute scene-radiance to ideal-image irradiance conversion,
spectral transmission, field RI and directional irradiance weights. It must
also generate a diffraction-aware PSF integrated at the selected sensor pitch
with the same focus, aperture, wavelength, image-height/reference-plane, and
scene-projection conditions. Those outputs need new pinned evidence and
validation; the geometric PSF and patent prescription alone cannot establish
them. Lens/sensor image coverage and CRA acceptance remain separate checks.
