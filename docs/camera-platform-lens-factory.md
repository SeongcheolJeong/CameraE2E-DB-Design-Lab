# Lens Model Factory: declared spherical prescriptions

`camera_platform.lens_factory` characterizes a centered prescription using actual
RayOptics 0.9.8 sequential ray tracing. It proposes a wavelength-resolved angular
response and relative-irradiance table for the platform's Response-DB consumer.
It does not certify a manufactured lens, image quality, or commercial rights.

The default `request_template()` is an authored synthetic singlet and separate
plane stop. Its five refractive indices are explicit illustrative inputs, not a
glass-catalog lookup or a vendor measurement. It covers the declared sensor
heights 0, 1.8 and 3.6 mm at 450, 500, 550, 600 and 650 nm.

## Pure interface

Install the optional dependency in the selected execution environment:

```sh
python -m pip install '.[platform,factory-rayoptics]'
```

The extra pins RayOptics 0.9.8 and opticalglass 1.2.0. Dependency resolution and
the execution environment remain deployment responsibilities; the adapter never
installs software, starts a GUI, fetches a material catalog, or modifies a source
prescription. Its imports are lazy, so other platform paths remain available
without the optional solver.

```python
from camera_platform.lens_factory import (
    characterize, describe_capabilities, request_template, validate_request,
)

request = validate_request(request_template())
result = characterize(request)
summary = result["summary"]            # finite JSON-compatible characterization
arrays = result["arrays"]              # NumPy evidence; preserve separately
payload = result["model_candidate"]    # five-key spectral LUT, or None
```

`validate_request()` returns a new normalized dictionary. Unsupported input,
missing backend, work-budget excess and elapsed compute limits raise
`LensFactoryError`; its `issues` list has `code`, `path` and `message` fields.
An executed but numerically unresolved characterization returns
`summary.status="REFUSED"`, preserved diagnostics and `model_candidate=None`.
A proposed table retains unknown physical uncertainty and unapproved status.

The adapter does not write a registry, approve or publish a model. The service
must preserve the original request, factory version, solver environment, arrays
and source/provenance records alongside an immutable model revision.

## Request contract

All keys below are required. Unknown keys are refused to avoid silently dropping
an unsupported asphere, coating, decenter or other physical input.

| Key | Meaning |
| --- | --- |
| `schema` | Exactly `lens_prescription_v1` |
| `length_unit`, `wavelength_unit` | Exactly `mm`, `nm` |
| `wavelength_nm` | 2–16 strictly increasing samples, 350–1100 nm |
| `surfaces` | 1–24 surfaces in forward object-to-sensor order |
| `field_radius` | 2–17 increasing radii from 0 to 1 |
| `image_height_mm` | Matching physical heights, exactly proportional to normalized radius |
| `illumination` | Explicit uniform object-space radiance, ideal lossless transmission and air at both ends |
| `sampling` | Declared quadrature dimensions and observed-agreement thresholds |

Each surface has `id`, `profile`, `radius_mm`, `thickness_mm`,
`semi_diameter_mm`, `refractive_index_after` and `is_stop`.

- `profile` is `spherical` with a nonzero signed radius, or `plane` with a null
  radius. A positive radius has its center of curvature toward positive z.
- `thickness_mm` is the positive axial distance to the next vertex. The final
  thickness reaches the fixed sensor plane. No automatic focus is performed.
- Each clear semi-diameter is explicit. Spherical apertures must remain inside
  their sphere's hemisphere; intersecting adjacent clear surfaces are rejected.
- `refractive_index_after` is an absolute real index for each exact requested
  wavelength, in the medium after the interface in the forward direction.
  There is no interpolation, nd/Vd approximation, inferred air gap or glass name
  substitution. The final medium must be air.
- Exactly one plane surface is marked `is_stop=true`, with equal index on both
  sides. Every surface aperture is checked; the stop marker does not bypass
  clipping by other surfaces.

The supported illumination declaration is:

```json
{
  "kind": "uniform_object_space_radiance",
  "transmission": "ideal_lossless",
  "object_medium": "air",
  "image_medium": "air"
}
```

The template's sampling declaration is:

```json
{
  "polar_rings": 24,
  "azimuth_samples": 24,
  "theta_bins": 32,
  "ri_tolerance_absolute": 0.03,
  "angular_cdf_tolerance_absolute": 0.05
}
```

The coarse pass uses `polar_rings * azimuth_samples` rays per wavelength and
field. The refined pass doubles both dimensions, using four times as many rays.
Total work, including both passes, is capped at 65,536 rays and 1,000,000
interface visits. Computation checks a 45-second deadline. The local worker
should additionally enforce its own process lifetime and recovery policy.

## Radiometry and sampling

An admitted-pupil count is a geometric throughput statistic. It is insufficient
as a sensor irradiance ratio. This matters in the existing RayOptics workbench:
its `min_pupil_throughput` is explicitly labeled geometric vignetting, and its
fallback illumination proxy cannot enter this factory as physical evidence.

For uniform external radiance and lossless optics with air at both endpoints,
the adapter evaluates the image-space integral

```text
E / L = integral[admitted directions] cos(theta) dOmega
      = integral[enclosing plane] visibility * cos(theta)^4 / d^2 dA
```

Here `d` is the positive distance between the parallel sensor and sampling
planes. The area-to-solid-angle weighting follows the camera measurement
equation in [PBRT, Realistic Cameras, sections 6.4.6–6.4.7](https://pbr-book.org/3ed-2018/Camera_Models/Realistic_Cameras).
The code uses [RayOptics' documented `trace_raw` interface](https://ray-optics.readthedocs.io/en/latest/api/rayoptics.raytr.raytrace.html)
for reverse refraction, surface intersections, aperture clipping and total
internal reflection. No source code from PBRT is copied.

The sampling plane lies at the greatest rear-surface sag, strictly before the
sensor. For sensor radius `h` and rear clear radius `a`, a disk of radius
`max(h,a)` contains the intersection of every sensor-to-rear-surface segment
with that plane: each intersection is a convex combination of the sensor point
and a rear-aperture point. This is a conservative bound, not a sampled pupil
boundary inferred from successful rays.

Off-axis fields use equal-area polar cells. On axis, declared rotational symmetry
allows azimuth to be integrated analytically: every traced radial sample
represents a complete equal-area annulus. The same ray budget provides more
radial resolution at hard aperture boundaries. This reduction is suitable for
the current azimuth-independent angular-response consumer; its ray artifacts
must not be interpreted as a sampled two-dimensional azimuth distribution.

Every attempted ray carries its area and cosine weight. A blocked or TIR ray
contributes zero to this ideal direct path. Invalid or non-finite traces prevent
a model proposal. No admitted samples means that the quadrature has not
established a transmitted pupil; it does not prove an exactly dark physical
field. Undefined normalization appears as null in the summary and is flagged;
zero-filled diagnostic array entries are not usable predictions.

## Candidate and evidence

The candidate has exactly these keys:

```text
wavelength_nm                 [W]
field_radius                  [F]
theta_deg                     [T], includes 0 and conservative upper support
angular_weights               [W,F,T], conditional incident-power weights
relative_illumination         [W,F], E(wavelength,field) / E(wavelength,center)
```

The theta nodes receive linearly distributed quadrature weights. This preserves
constant and linear angular responses on the selected basis. It is a finite
angular representation, not a continuous scattering function or diffraction
model. Each nonempty conditional distribution sums to one. Relative illumination
is computed independently from the unnormalized irradiance integrals; it can
exceed one and is never clamped to make a conventional-looking curve.

The consumer must use the exact same wavelength axis. Collapsing these tables
into wavelength-invariant angular weights would erase declared dispersion.
The relative-illumination term is applied once, separately from conditional
angle weights, before the sensor response is integrated.

Evidence arrays include irradiance per radiance in sr, angular weights, raw
visibility masks, per-category failure counts, plane sample coordinates,
quadrature weights and image/object direction cosines. Array dimensions are:

| Array | Shape / interpretation |
| --- | --- |
| `accepted_rays` | `[W,F,N]`, refined visibility mask |
| `image_direction_cosines` | `[F,N,3]`, forward light direction at sensor |
| `object_exit_direction_cosines` | `[W,F,N,3]`, reverse tracing direction exiting toward object; zero if rejected |
| `plane_sample_xy_mm` | `[F,N,2]`, centered sampling-plane coordinates |
| `quadrature_weights_sr` | `[F,N]`, before visibility |
| `failure_counts` | `[W,F,4]`: aperture blocked, TIR, missed surface, invalid trace |
| `irradiance_per_radiance_sr` | `[W,F]`, refined numerical integral |
| `coarse_irradiance_per_radiance_sr`, `coarse_angular_weights` | Evidence for the second sampling level |

Proposal requires positive coarse and refined throughput in every requested
field, no invalid trace, and the declared relative-illumination and angular-CDF
agreement thresholds. Two-level differences are numerical observations, not
rigorous error bounds or physical prediction uncertainty. Both grids can share
an unresolved feature; a more demanding use requires stronger convergence
studies and independent references.

The reported diameter `2 * max(image_height_mm)` describes only requested
sampling coverage. It is not an inferred quality-qualified image circle.
The module does not produce EFL, f-number, mount dimensions, PSF, MTF, distortion,
coatings, Fresnel losses, absorption, flare, finite-scene radiance or tolerances.
These quantities require separate declared data and implementations. Lens or
source-file licensing remains subject to the platform's provenance review.

The verification suite uses actual synthetic traces, a circular-aperture
analytical irradiance integral, reciprocity, geometric scaling, deterministic
replay, dispersion sensitivity, bounded execution and refusal cases. Such tests
verify implementation behavior; they do not establish agreement with hardware.
