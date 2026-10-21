# Centered even-asphere optics — dev14 implementation contract

Status: bounded numerical and application workflow verified in 0.16.0.dev14.
This continuation extends the general optical model while retaining all seven
platform modules and four execution modes. Installed primary remains 0.15.0;
this milestone does not close the commercial platform objective.

## Physical contract

`lens_prescription_v4` extends the explicit v3 prescription with centered,
rotationally symmetric even-asphere surfaces. Old v1–v3 surface meaning and
normalized inputs remain unchanged. Plane and spherical surfaces retain their
existing keys. An `even_asphere` surface has the same base keys plus:

```json
"asphere": {
  "schema": "camera_even_asphere_v1",
  "conic_constant": -1.0,
  "orders": [4, 6, 8],
  "coefficients": [0.0005, 0.0, 0.0],
  "coefficient_units": ["mm^-3", "mm^-5", "mm^-7"]
}
```

The signed finite nonzero vertex radius defines `c=1/radius_mm`. All spatial
coordinates are mm. The sag is
`z(r)=c*r²/(1+sqrt(1-(1+k)*c²*r²))+A4*r⁴+A6*r⁶+A8*r⁸`.
There is no quadratic polynomial coefficient: the authored radius retains the
paraxial vertex curvature. RayOptics' coefficient array starts at A2, so the
adapter must map to `[0,A4,A6,A8]`. The authored stop remains an unpowered plane;
an aspheric refracting surface cannot silently become the stop.

One shared geometry module defines the surface for reciprocal radiometry and
forward OPD/PSF tracing. The full prescription, including each coefficient and
unit, is hashed and preserved in both Factory jobs and model lineage.

## Geometry and ray validity

The conic radicand must remain positive over the entire clear aperture. Sag and
slope must be finite and bounded. Adjacent surfaces must remain separated across
their full common radial aperture; checking only vertices and edges is not
sufficient for a nonmonotonic polynomial. Conservative interval enclosures and
bounded subdivision certify admitted separation. Unresolved geometry is refused.
The sensor plane and reciprocal quadrature's enclosing plane use an upper bound
on the entire rear sag, rather than the rear edge sag.

Surface intersection must identify the correct sheet and a unique admitted
intersection. Iterative solver success or a small surface residual alone does not
prove that. Safeguarded intersection and direction/slope admission reject
ambiguous geometry instead of counting it as physical vignetting. RayOptics may
shift a ray's local origin; signed local intersection distance is distinct from
the nonnegative physical segment between successive surfaces.

## Factory, runtime and evidence

`prescription_field_psf_v4` embeds exactly the same v4 prescription. Existing
stop-power integration and square-root pupil amplitude remain in place.
Reciprocal rays provide absolute E/L, relative illumination and the actual
direction/power distribution. The new directional response schema is
`camera_lens_directional_response_v2`, with symmetry explicitly allowing centered
even aspheres. Existing v1 directional records retain their spherical/plane
declaration. Native pixel integration uses the same direction quadrature.

The resulting field PSF remains `field_intensity_psf_v1`: scalar paraxial exit
pupil propagation, fixed sensor plane, infinity plane-wave object and conditional
unit-sum finite kernels. Existing convergence, tail, cone and mapping thresholds
must not be relaxed to accept an asphere. General depth, nonparaxial amplitude
transport, tilt/decenter, freeform optics, Fresnel/coatings, polarization and
stray light remain separate work. A new lens shape does not remove those limits.

Workbench exposes explicit v4 templates through the existing Factory workflow.
Publication binds the exact radiometry and PSF prescriptions, then ordinary
Camera composition, spectral runtime, RAW/ISP and evidence bundles consume the
new model. No automatic physical-validation or approval promotion is allowed.

The native direction/EQE path still has a 128-million weighted-sample execution
budget. With this milestone's detailed quadrature, the spherical-limit case costs
185,416 weighted samples per pixel and the asphere case costs 185,568. Both 64×64
and 32×32 exceed the budget. A 16×16 native crop uses 47,466,496 and 47,505,408
samples respectively, reflecting different admitted rays. A refused input is retained; a smaller
scene and Test are appended as new immutable records. The optical sampling and
acceptance thresholds are not weakened to accommodate the image size. Full-frame
performance is not established by this small-crop integration proof.

The matched fixture supplies identical ideal on-axis image irradiance that
already includes on-axis throughput. It tests shape-dependent field PSF and
directional pixel response under fixed acquisition conditions. It is not a
general scene-radiance-to-sensor projection or an absolute physical throughput
validation.

The reciprocal image-side sampling directions can remain identical when the
rear surface and enclosing plane are unchanged. They are quadrature coordinates,
not an independent forward-trace result. Shape changes are assessed through
object-exit directions on common admitted rays, separately counted admission
changes, directional weights, forward sensor intersections, OPD/PSF and actual
pixel/RAW outputs. A fixed sampling grid must not be mistaken for an unchanged
optical response.

## Acceptance

- Zero conic/polynomial departure reproduces the corresponding spherical surface
  and existing v1–v3 records retain their original meaning and content.
- Independent analytic conic roots, sag/normal formulas, separate bracket/Snell
  tracing, reciprocal paths and geometric scaling verify coordinates and units.
- Interior intersection and rear-sag extrema counterexamples exercise geometry
  checks; domain, nonfinite, malformed and ambiguous-ray requests are refused.
- Nonzero aspheres change actual ray directions, OPD/PSF and RAW under fixed
  acquisition conditions; exported intermediate arrays preserve the evidence.
- Actual API/Worker and browser Factory/model workflows, restart readback,
  source binding, old record preservation and backup restoration are verified.

All initial fixtures are authored numerical designs. Physical camera accuracy,
total uncertainty and customer-installation acceptance remain unestablished.

## Verification evidence

The [milestone evidence](../reports/camera_platform/aspheric-optics-v016/evidence-summary.json)
binds the frozen source, independent geometry checks and actual API/Worker flows.
All 3,736 unique platform tests passed after targeted reruns of 47 tests whose
local socket/process access was denied by the sandbox. The original full-run
failure report is retained; product source did not change for these reruns.
There are also 171 mock-DOM checks, distinct from actual CUA browser verification.

The HTTP proof passed 552 checks and its separate restart readback passed 807.
Four optical Factory jobs produced two matched lens models, two spatial RAW runs
and one exact-RAW-preserving ISP replay. Three bundles retain 270 exact members.
The authored fixture changes expected charge by up to 79.059 electrons and RAW
by up to 93 DN across two frames. These are changes between numerical designs,
not measured accuracy or a claim that the asphere improves image quality.

Actual browser actions submitted one additional v4 radiometry Factory job and
published its draft lens model; they did not run the PSF/RAW workflow. Browser
records and artifacts were verified again after restart, and the reloaded UI
showed the published version. Existing 94 records, 35 jobs, 199 audit rows,
1,727 blobs and five historical bundles remained unchanged. The final state of
110 records, 43 jobs, 239 audit rows and 2,044 blobs was backed up and exactly
restored in a new directory. The owned QA API/Worker and browser tab were closed.

Reference: the installed RayOptics 0.9.8 profile implementation and its
[official surface-profile documentation](https://ray-optics.readthedocs.io/en/stable/api/rayoptics.elem.profiles.html).
