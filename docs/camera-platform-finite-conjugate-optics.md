# Finite-conjugate optics — source implementation contract

Status: bounded numerical and local service/runtime checks completed for
`0.16.1.dev7`. This adds an actual finite-object optical path to the existing
general-optics work: object distance changes traced wavefronts and the resulting
sensor input and RAW. It retains the seven platform modules and four execution
modes. It does not qualify a new installation or complete the commercial product
objective. Installed primary 0.15.0 and the separately preserved installed
0.16.0 candidate are outside this source-only qualification.

Owned HTTP/Worker-process, browser and restart checks subsequently completed for
this source slice; their scope and original evidence are linked below. These
operational checks do not establish physical-camera accuracy or installation readiness.

## Authored object geometry

`lens_prescription_v5` extends the existing centered plane, spherical and
even-asphere prescription with exactly this object contract:

```json
"object_conjugate": {
  "schema": "camera_finite_planar_object_v1",
  "distance_mm": 500.0,
  "reference": "first_surface_vertex",
  "orientation": "normal_toward_sensor",
  "support": "unbounded_uniform_lambertian_plane",
  "reference_magnification": -0.04104715527949275
}
```

Distance is positive, measured from the first physical surface vertex toward the
object, with an admitted range of `1e-3..1e6 mm`. With the optical axis pointing
toward the sensor, the object plane is `z = -distance_mm`. Clearance is certified
against the entire first-surface sag bound, not only its vertex.

The authored signed reference magnification is in `[-10, -1e-6]` and is shared by
all wavelengths. It defines the explicit two-dimensional chart
`ideal_sensor_xy = reference_magnification * object_xy`. It neither focuses the
lens nor substitutes effective focal length for image distance. The fixed chart
also does not recenter each wavelength's chief ray, so lateral chromatic shifts
and distortion are not silently removed. A change of distance can therefore
introduce defocus at the same physical sensor plane.

Published finite Lens models carry exact `object_distance_mm` (mm) and
`object_reference_magnification` (unit 1) conditions matching the original
prescription. Scene creation and simulation verify that binding; a different
distance or magnification requires an appropriately characterized Lens.

## Radiometry and the finite wavefront

The existing `rayoptics_lens_v1` plugin emits
`camera_lens_radiometry_v2` and, when the joint directional response is requested,
`camera_lens_directional_response_v3`. Both preserve the complete object
conjugate. Reciprocal rays now intersect the actual object plane and save
`object_exit_position_xyz_mm`, `object_plane_xy_mm`,
`object_plane_ray_distance_mm` and `object_plane_valid`, alongside the existing
direction and power evidence. Invalid numerical intersections refuse the
candidate; they are not converted into physical vignetting. These extra arrays
and the additional plane-intersection work are included in resource bounds.

Absolute irradiance per unit radiance, E/L, remains an image-space reciprocal
integral for a uniform Lambertian calibration plane. There is no extra
`1 / distance_mm²` multiplier or object-side cosine multiplier. Directional
weights remain conditional transmitted irradiance fractions at the sensor,
before the pixel-optics response. Saved object-plane footprints are diagnostic
evidence; sampling texture over them and then applying the same optical PSF
would apply the blur twice.

`prescription_field_psf_v5`, handled by the existing `prescription_psf_v1` plugin,
embeds that same v5 prescription and declares
`object_conjugate: "finite_planar_object"`. Actual rays start at a common finite
object point, pass through the authored material segments and stop, and reach
the fixed sensor/reference geometry. OPD uses the finite object path rather
than an infinity plane wave. Its sign convention is chief optical path minus
sampled optical path, with propagation phase `exp(-2πi OPD / wavelength)`.

The output remains a conditional, unit-sum `field_intensity_psf_v1` with scalar
paraxial exit-pupil propagation and approximate amplitude transport. Existing
phase, cone, mapping, convergence, retained-energy and pixel-integration gates
remain in force. Excessive defocus can fail those gates; it is not accepted by
automatically moving the sensor or relaxing the thresholds.

Two finite-only corrections address behavior reproduced with installed
RayOptics 0.9.8:

1. Relative-pupil tracing can launch a finite field from the opposite object
   height. The finite adapter explicitly aims chief and sampled rays and checks
   their actual common object origin.
2. The axial exit-pupil fallback can mix the actual sensor-plane paraxial ray
   height with a paraxial conjugate image distance. Moving the sensor then
   incorrectly moves the inferred pupil. The finite adapter uses the chief-ray
   intercept from the last physical surface before exit-pupil transfer.

Independent tests compare signed ABCD conjugates, actual ray origins, direct
Euclidean segment optical paths and reference-sphere intersections, reciprocal
plane hits, and an analytic disk solid-angle irradiance result. The exit-pupil
test changes sensor gap while requiring the physical pupil to remain fixed.
These checks and the two corrections are recorded in the
[independent review](../reports/camera_platform/finite-conjugate-optics-v0161/independent-review.json).

## Bounded plane-to-RAW workflow

The v5 PSF template uses distance 500 mm, reference magnification
`-0.04104715527949275`, and a rear physical surface-to-sensor gap of
`19.592456537208645 mm`. Its declared reference focal length
`19.812067271756955 mm` remains an f-number reference, not the finite chart
mapping. The default wavelengths are 500 and 600 nm.

The local integration fixture characterizes two separate Lens models at 500 and
600 mm, retaining that same sensor gap and reference magnification. It uses a
16×16 sensor with 3 µm pitch and a bounded PSF field grid. The authored spectral
radiance chart is identical in the two runs. The test verifies different blurred
spectral irradiance and different actual RAW arrays, rather than relying only
on changed distance metadata. It executes the simulation through local service
and registry methods; it is not a separate HTTP Worker-process test.

| Fixed-sensor fixture | PSF convergence kernel L1 | Minimum retained energy | Factory outcome |
| --- | ---: | ---: | --- |
| 500 mm | 0.0264783 | 0.963932 | PROPOSED |
| 600 mm | 0.0271838 | 0.963168 | PROPOSED |

These authored fixture results are saved in
[PSF workflow fixtures](../reports/camera_platform/finite-conjugate-optics-v0161/psf-workflow-fixtures.json).
`PROPOSED` is a Factory result, not measured optical accuracy or model approval.

`camera_finite_plane_fixture_v1` authors a flat, slanted-edge or disk radiance
pattern in physical object-plane coordinates. Even image dimensions from 8 to
64 use the assembly's native pixel pitch and wavelength grid. The chart is
sampled at nominal pixel centers and multiplied once by verified on-axis E/L.
The runtime then applies the existing conditional PSF, receiver relative
illumination and angular QE. There is no second defocus operation.

The scene stores object coordinates, spectral radiance, ideal spectral
irradiance and a projection declaration. Verification binds their original
bytes and recomputed values to the exact Lens identity and conjugate before the
spatial, sequence or control runtime consumes them. Generic spectral import
cannot claim this reserved finite-plane provenance.

The unbounded uniform plane in the conjugate is the **radiometry calibration
support**. The application fixture is an illuminated finite input rectangle:
radiance is zero outside the supplied nominal image window, there is no source
halo, and PSF tails leaving that window are lost. Even a flat fixture is windowed.
A kernel larger than the input can affect its center as well as its boundary;
these tests do not establish infinite uniform-plane RAW brightness. Native
source-center sampling is also not continuous source-cell integration.

## API and workbench wiring

Existing Factory routes accept v5 templates:

- `GET /api/v1/templates/factory?plugin_id=rayoptics_lens_v1&request_version=5`
- `GET /api/v1/templates/factory?plugin_id=prescription_psf_v1&request_version=5`
- `POST /api/v1/factory/jobs`, followed by the existing job detail and
  `POST /api/v1/factory/models` publication flow. Composite PSF publication binds
  the exact radiometry parent Lens.
- `GET /api/v1/templates/spatial-scene?assembly_id=...` selects the finite-plane
  fixture schema for a finite Lens.
- `POST /api/v1/spectral_images/finite_plane` creates the immutable plane input;
  existing spatial-Test and `spatial_scene_v1` run routes consume it.

The Factory UI exposes “유한 물체면 · 거리·배율 고정 형식 ↙” for both optical
adapters. Publication carries the authored distance and magnification into model
conditions. The finite scene form describes those fixed values, zero exterior
radiance, missing halo and sampling approximation. The owned development API/Worker and actual browser path were exercised: four
Factory jobs, two native runs and one retained-RAW ISP replay. The browser
registered an additional disk chart and loaded the saved 16×16 result. After
restart, nine selected records and all 264 ZIP members from three bundles were
identical; the same PNG loaded again. Both owned API/Worker phases and the
temporary browser tab were closed. The installed primary DB and PID were
unchanged within both QA intervals. See the
[final evidence summary](../reports/camera_platform/finite-conjugate-optics-v0161/evidence-summary.json).

All current 3D projection paths are refused for a finite Lens. Existing pinhole,
stored-radiance and manual-transmittance routes cannot silently substitute an
infinity/EFL projection for this finite physical chart.

## Preserved behavior, verification and limits

Legacy v1–v4 prescription and PSF schemas keep their existing meaning. Eight
saved radiometry cases compare complete arrays, candidates and summaries exactly
before and after the extension. Separate-process PSF comparisons preserve full
v1–v4 output fingerprints under the same dependencies. See
[geometry verification](../reports/camera_platform/finite-conjugate-optics-v0161/geometry-verification.json)
and [PSF legacy parity](../reports/camera_platform/finite-conjugate-optics-v0161/psf-legacy-parity.json).

The focused test inventory contains **223 distinct Python cases** and four
Node UI checks. These are not full-platform regression or physical-validation
counts. Numerical and workflow suites retain their individual scopes:

| Evidence | Result | Scope |
| --- | --- | --- |
| [Geometry suite](../reports/camera_platform/finite-conjugate-optics-v0161/geometry-final.xml) | 102 passed | 33 new finite cases and 69 existing cases |
| [PSF suite](../reports/camera_platform/finite-conjugate-optics-v0161/psf-focused.xml) | 51 passed | 15 new cases and 36 existing cases |
| [Independent numerical suite](../reports/camera_platform/finite-conjugate-optics-v0161/independent-final-tests.xml) | 21 passed | ABCD, direct OPL, origins, reciprocal geometry and radiometry |
| [Local workflow suite](../reports/camera_platform/finite-conjugate-optics-v0161/workflow-tests.txt) | 15 passed, 6 warnings | Finite input binding/refusals and actual local simulation arrays |
| [Legacy integration](../reports/camera_platform/finite-conjugate-optics-v0161/legacy-integration-tests.xml) | 34 passed | Factory, imaging API, radiometry and scene projection API |

This advances the general-optics priority beyond an infinity-only source and
connects it to existing pixel/electrical simulation. It does not add arbitrary
depth, tilt/decenter, freeform surfaces, full 3D transport, Fresnel/coating or
polarization response, stray light, or exact joint spatial/angular transport.
The conditional PSF followed by receiver radiometry/angular QE is an explicit
factorization approximation, especially for textured scenes. HDR/LFM and ISP
capabilities do not gain new physical qualifications merely by using this Lens.

Physical measurement cases for this extension: **0**. Physical validation and
commercial optical accuracy: **not established**. Combined model uncertainty:
**unknown**. Source-only numerical acceptance, installation qualification and
physical-camera qualification remain separate claims.
