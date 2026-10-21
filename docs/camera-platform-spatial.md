# Native spectral image and PSF runtime

`camera_platform.spatial.SpatialSceneRuntime`, plugin ID `spatial_scene_v1`,
connects a supplied spectral ideal image to an explicit intensity PSF, radial
lens/sensor response, real `pyisetcam` digital readout, and the shared
[software ISP](camera-platform-isp-replay.md). It implements a bounded spatial path
toward the full platform requirements; it is not a 3D scene renderer, a measured
camera model, or a commercial accuracy qualification.

The runtime implements `describe_capabilities()`, `validate_request(request)`,
`estimate_resources(request)`, and `execute(request)`. Execution returns
`{"summary": <JSON-compatible dict>, "arrays": <NumPy-array dict>}`.
`RuntimeRequestError.issues` describes refusals. The registry/service owns
immutable source records, job scheduling, and artifact persistence; this module
does not read arbitrary files or write primary state.

## Executable example

```python
from camera_platform.isp import default_profile
from camera_platform.spatial import SpatialSceneRuntime, request_template

request = request_template()
request["isp_profile"] = default_profile()
request["frames"] = 4
request["seed"] = 223

plugin = SpatialSceneRuntime()
assert plugin.validate_request(request) == []
estimate = plugin.estimate_resources(request)
result = plugin.execute(request)
raw = result["arrays"]["raw_frames"]       # integer [4, 64, 64], digital DN
rgb = result["arrays"]["rgb"]              # float [64, 64, 3], first frame
prediction = result["summary"]["prediction"]
assert prediction["uncertainty"] == {"kind": "unknown"}
```

The template is an authored step chart with five declared spectral bands and
assumed binomial pixel-hit probabilities. It contains no vendor data or
measurement. Its `estimated` sources describe those assumptions explicitly.
Changing a template label to `measured` does not establish a physical result;
the service must retain the real input source and its provenance.

## Input contract

Common top-level inputs reuse the existing runtime's validated lens/QE/readout
contract: `wavelength_nm`, `qe[channel,wavelength]`, `pixel_pitch_um`,
`exposure_s`, `readout`, `lens`, and `sensor_angular_response`. Optional fields
are `temperature_c`, `frames`, `seed`, `noise_enabled`, and `isp_profile`.
`scene` and `psf` are required. Unknown top-level fields, including
`output_size`, are refused: the supplied image defines the actual pixel grid.

QE means collected electrons per incident photon, including the already
specified CFA/microlens/collection response. Optical absorption alone is not
QE. Readout requires exactly `full_well_e`, `read_noise_e`,
`dark_current_e_per_s`, `gain`, `bit_depth`, and `black_level_dn`.

`scene` has exactly these fields:

| Field | Required meaning |
| --- | --- |
| `schema` | `spectral_ideal_image_v1` |
| `wavelength_nm` | Exactly the top-level wavelength coordinates |
| `unit` | `W/m^2/nm` |
| `reference_plane` | `sensor_ideal_image_before_psf_ri` |
| `spectral_irradiance` | Finite nonnegative real array `[rows, columns, wavelength]` |
| `sampling` | `{kind: "native_sensor", full_sensor_shape: [H, W], crop_origin: [row, col], pixel_pitch_um: <sensor pitch>}`; optional `capture_crop` as described below |
| `included_effects` | Exactly `["on_axis_lens_throughput"]` |
| `source` | `{kind: "measured" | "simulated" | "estimated", description: <nonempty text>}` |

This input is ideal-image irradiance after on-axis aperture/transmission
throughput, before the supplied PSF and relative illumination. It is not scene
radiance in `W/m²/sr/nm`. No RGB-to-spectrum conversion, f-number conversion, or
implicit second transmission multiplier exists. A future radiance adapter must
declare its geometry and throughput mapping separately.

`psf` has exactly these fields:

| Field | Required meaning |
| --- | --- |
| `schema` | `intensity_psf_v1` |
| `wavelength_nm` | Exactly the top-level wavelength coordinates |
| `values` | Finite nonnegative array `[wavelength, kernel_rows, kernel_columns]` |
| `sample_pitch_um` | Exactly the native sensor/scene pitch |
| `normalization` | `unit_sum`; each wavelength sums to one within absolute tolerance `1e-10` |
| `centering` | `array_center`; the middle array element is zero displacement |
| `sampling_model` | `pixel_integrated_optical_impulse` |
| `spatial_model` | `locally_shift_invariant` |
| `boundary` | `zero_outside_supplied_image` |
| `source` | Same explicit source-description structure as the scene |

PSF values are discrete probabilities of optical impulse power reaching each
receiving native pixel. Receiving-pixel aperture integration is already inside
this kernel, and occurs once. Each input cell's flux is assigned to its center
as a source quadrature approximation. This is not exact integration of an
extended piecewise-constant source cell and cannot recover aliased input
content. There is no additional pixel box filter or automatic PSF scaling.

An MTF magnitude is not an intensity PSF and cannot be supplied in its place.
An OTF computed from this pixel-integrated discrete kernel includes its aperture
and sampling convention; it is not the continuous optical MTF or an observed
RAW/ISP SFR. A PSF factory's finite-window captured-energy fraction and
convergence evidence must remain attached to its immutable source. The runtime
does not invent omitted tails or convert truncation into known physical accuracy.

The lens supports the existing wavelength-independent four-key LUT and the
five-key wavelength-resolved LUT. The latter must exactly match the runtime's
wavelength grid. Angular weights are discrete power fractions, so there is no
second `sin(theta)` weighting. Angles must lie within the sensor response
domain. RI can exceed one because it is relative to the on-axis reference,
not an absolute transmission efficiency.

## Calculation and spatial boundary

For every wavelength, the runtime computes the full discrete linear convolution
of input irradiance and the supplied kernel, then takes the same-size crop
centered on the kernel origin. It uses `scipy.signal.fftconvolve(..., mode="full")`;
the explicit crop implements the same boundary convention described in the
[SciPy reference](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.fftconvolve.html).
The exterior of the supplied image is dark, including when it is a crop inside
a larger sensor. Light can leave the retained crop; it never wraps around to
the opposite edge. A larger input with adequate context is necessary when a
real scene has light just outside that crop.

For a smaller receiver ROI, opt in with
`scene.sampling.capture_crop = {"crop_origin": [native_row, native_col], "shape": [rows, cols]}`.
The original `spectral_irradiance` and `sampling.crop_origin` then describe the
larger **source support**. Without carrier collection, the source must cover
the receiver ROI plus the PSF kernel radius on every side still inside the
physical sensor. Preflight rejects missing halo pixels rather than inventing
their irradiance. PSF convolution runs
over the source support. Without charge collection, RI and pixel response run
on the receiver ROI; with charge collection, generation and routing run over
the source support. Readout and ISP always run on the receiver ROI at its
absolute native coordinates and CFA phase. The result records both geometries,
separate source/capture flux, and each array's geometry. A
registered `spatial_scene_v1` run may pass the same `capture_crop` in its
parameters; the Worker reconstructs it from the frozen source and run request.

With `charge_collection`, a partial source must additionally cover every
generation site whose carrier offsets can reach the receiver ROI, plus the Lens
PSF radius around those sites. Preflight computes this union in absolute native
coordinates and refuses missing support. PSF, charge generation and carrier
routing run on the supplied source before cropping to the receiver. Collection
outside the supplied support but inside the sensor is recorded separately from
physical off-sensor loss. The source-support collection map may omit incoming
contributions from unprovided exterior pixels; the verified receiver ROI has
complete incoming support under the declared PSF and collection model.

For a registered 3D scene render, `render_psf_halo: true` expands the requested
`shape` and `crop_origin` using the pinned Lens PSF. For generated charge and
cross-site collection, use `render_charge_collection_halo: true`; submission also
pins the Sensor ID, model hash, collection artifact and destination offsets.
Both render modes store the requested receiver as `capture_crop`. Publishing
preserves its frozen render snapshot, and a subsequent spatial run defaults an
omitted `parameters.capture_crop` to that receiver after checking the binding.
Imported images must supply an explicit valid halo and receiver crop. ISP
demosaic at the cropped RAW boundary can differ from full-frame ISP followed
by image cropping.

The full convolution must conserve per-wavelength discrete flux before RI:

```text
input flux [W/nm] = sum(image irradiance [W/m²/nm]) × pixel area [m²]
full output flux = input flux × supplied kernel sum
```

Tiny negative FFT roundoff below the documented relative threshold `1e-12` is
trimmed to zero, with its removed magnitude recorded. Larger negatives or
nonfinite values are refused. Full-convolution flux is checked with relative
tolerance `1e-9`. The summary retains input, full, retained, and outside-crop
spectral flux plus the roundoff correction. The irradiance/area/flux distinction
follows the primary [radiometry definitions](https://pbr-book.org/3ed-2018/Color_and_Radiometry/Radiometry).

Native pixel centers use the actual full sensor shape and crop origin. Radius
one is the full sensor's corner-pixel center, not the crop corner. Odd crop
origins preserve the full sensor's RGGB phase. Pixel pitch sets physical cell
area and must agree across image, sensor, and PSF. No aspect-ratio changes,
resize, spatial resampling, or representative raster mapping are performed.

At each native position, the runtime linearly interpolates RI and
angle-averaged effective QE independently from the declared field points.
It multiplies the blurred irradiance by those responses and integrates:

```text
electrons[channel, y, x] = pixel_area × exposure_s
  × integral_nm(E_blurred[y,x,lambda] × RI[y,x,lambda]
                × QE_effective[channel,y,x,lambda]
                × lambda_m / (Planck_J_s × speed_of_light_m_s))
```

Wavelength integration is trapezoidal. Applying one PSF before output-position
RI and angle/QE is an explicit separable local approximation. General coupled
field-dependent PSF/angular transport, distortion, polarization, azimuth,
crosstalk, subpixel scene detail, motion, HDR, and 3D occlusion remain unsupported.

The real `pyisetcam.configure_e2e_sensor` and `resample_sensor_capture` entry
points then apply mean dark charge, optional Poisson/read noise, well clipping,
gain, ADC clipping/rounding, and black pedestal. PRNU/DSNU are explicitly zero.
Changing gain leaves the incident electron map unchanged. Independent seeded
frames represent capture noise, not scene motion or exposure-control behavior.

## Outputs and interpretation

Artifacts preserve the ideal and blurred spectral images, sensor-plane spectral
image after RI, PSF, RI/radius/CFA maps, three-channel electron map, actual
mosaic mean electrons, expected DN maps, integer RAW frames, every shared ISP
stage, and RGB frames. `summary.array_units` maps every artifact to its unit.
`summary.geometry`, `spatial_sampling`, `readout`, `conditions`, source
descriptions, seed sequence, and capture metadata preserve the interpretation.

The prediction is `raw.channel_mean_dn`, unit `DN`, with channel axis `[R,G,B]`.
It averages the continuous clipped, dark- and black-corrected expectation at
each channel's actual native CFA sites. It is distinct from the sampled RAW
realizations and does not include ADC rounding or a claimed error bound.
Nonuniform scenes do not produce an invented radial channel-ratio curve.
Observed RAW and simulation need an explicitly matched observation operator
before measurement comparison.

`physical_validation` stays `not_established`, and prediction uncertainty stays
`unknown`. A temporal noise estimate uses per-pixel variance across frames;
spatial variation in the scene is never substituted for noise. Saturation above
0.1% withholds the temporal SNR estimate. This is not an EMVA characterization.

## Limits and verification

The local runtime accepts 2–16 wavelength samples, even image dimensions
8–512, 1–16 frames, at most 1,048,576 total frame-pixels, and odd PSF sides 1–65.
Scene/PSF decoded real arrays are limited to 64 MiB, with a separate spectral
sample bound. A conservative incident-charge limit protects the sampler.
Working-array estimates are declared budgets, not measured peak memory.
The API may impose smaller transport limits independently.

The tests in `tests/platform/test_spatial_runtime.py` use explicitly synthetic
fixtures. They verify delta identity, asymmetric convolution orientation,
chromatic kernels, exact edge-flux loss, native crop/CFA equivalence, independent
spectral response integration, area/exposure scaling, gain separation, actual
RAW/RGB changes, reproducible temporal noise, ISP-only tuning, and strict input
refusals. These checks establish implementation behavior within this contract;
they do not establish physical camera accuracy.
