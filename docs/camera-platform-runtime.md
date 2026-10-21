# Camera platform runtime contract

`src/camera_platform/runtime.py` provides two replaceable runtime plugins. Both
return numerical predictions with explicit assumptions; neither claims that a
physical camera has been validated. The existing `pyisetcam` readout and ISP
implementation remains an execution dependency of the Response-DB adapter.

| Plugin | Current behavior | Execution dependency |
| --- | --- | --- |
| `quick_estimate_v1` (`quick_estimate`) | On-axis expected photoelectrons, idealized SNR and saturation margin | NumPy |
| `response_db_v1` (`response_db`) | Angular-response integration, radial flat-field shading, seeded digital RAW and first-frame sensor RGB | NumPy and existing CameraE2E engine/assets |

## Calling a plugin

```python
from camera_platform.runtime import get_plugin, run_plugin

plugin = get_plugin("response_db")
capabilities = plugin.describe_capabilities()
issues = plugin.validate_request(request)
resources = plugin.estimate_resources(request)
output = run_plugin("response_db", request)
summary = output["summary"]        # JSON-compatible metadata and small curves
arrays = output["arrays"]          # NumPy arrays; persist as separate artifacts
```

`validate_request()` returns a list of `{code, field, message, severity}` issues.
Execution and resource estimation reject invalid requests with
`RuntimeRequestError`, a `ValueError` carrying the same list as `.issues`. Unknown
plugin IDs produce `UNSUPPORTED_RUNTIME`; they never select another backend.
Requests with lens angles outside sensor coverage produce `OUT_OF_DOMAIN`.

`describe_capabilities().available` for Response-DB indicates Python module
discovery only. An executable Python environment and the existing CameraE2E
spectral/display assets must also be provisioned. Missing engine dependencies or
assets remain execution errors. Resource estimates report a bounded array-memory
estimate, not measured process peak memory or a performance guarantee.

The application service resolves immutable lens/sensor/ISP revisions and a test
revision into this request. It records the complete request, model snapshots,
source-code hashes, package versions, seed, and artifact hashes. The low-level
runtime API itself does not implement authorization, asset approval, or storage.
The test stimulus must declare `wavelength_unit: "nm"` and
`spectral_irradiance_unit: "W/m^2/nm"`, with `reference_plane: "sensor_surface"`.
Missing or contradictory units are refused before submission; the service maps
this explicitly on-axis stimulus into the runtime's unit-bearing field names.

## Complete request example

This is an authored analytic fixture for implementation testing. It is not a
vendor QE curve, lens prescription, or measurement.

```python
request = {
    "wavelength_nm": [400, 500, 600, 700],
    "spectral_irradiance_w_m2_nm": [0.00001] * 4,
    "irradiance_reference_plane": "sensor_surface_on_axis",
    "qe": [[0.3] * 4, [0.5] * 4, [0.2] * 4],
    "pixel_pitch_um": 4.0,
    "exposure_s": 0.004,
    "temperature_c": 25.0,
    "readout": {
        "full_well_e": 10000.0,
        "read_noise_e": 2.0,
        "dark_current_e_per_s": 0.0,
        "gain": 1.0,
        "bit_depth": 16,
        "black_level_dn": 256.0,
    },
    "lens": {
        "field_radius": [0.0, 0.5, 1.0],
        "theta_deg": [0.0, 20.0],
        "angular_weights": [[1.0, 0.0], [0.5, 0.5], [0.0, 1.0]],
        "relative_illumination": [1.0, 0.9, 0.7],
    },
    "sensor_angular_response": {
        "theta_deg": [0.0, 20.0],
        "relative_response": [
            [[1.0, 0.5]] * 4,
            [[1.0, 1.0]] * 4,
            [[1.0, 0.8]] * 4,
        ],
    },
    "output_size": [32, 32],
    "frames": 8,
    "seed": 120,
    "noise_enabled": True,
}
```

Quick Estimate consumes the common spectral/readout fields and evaluates normal
incidence only. Its calculation does not use lens angular weights or spatial
illumination. The application currently accepts the narrower declared
unpacked RGGB12/black-64 contract; the low-level readout adapter supports the bit
depth and black level shown above.

## Physical meaning and array axes

| Input | Axis order / unit | Required meaning |
| --- | --- | --- |
| `wavelength_nm` | `[wavelength]`, nm | Strictly increasing, common to irradiance and QE |
| `spectral_irradiance_w_m2_nm` | `[wavelength]`, W m⁻² nm⁻¹ | On-axis sensor-entrance irradiance, after lens transmission and aperture throughput |
| `qe` | `[R,G,B][wavelength]`, electron/photon | Normal-incidence collected-electron QE, including CFA, microlens and charge collection |
| `pixel_pitch_um` | µm | Square native pixel pitch; its full geometrical area is used with effective QE |
| `exposure_s` | s | Fixed scalar exposure for all generated frames |
| `angular_weights` | `[field_radius][lens theta]`, unitless | Power fraction per angular bin, already including bin integration |
| `relative_illumination` | `[field_radius]`, unitless | Spatial throughput relative to the on-axis irradiance, exactly 1 at field 0 |
| `relative_response` | `[R,G,B][wavelength][sensor theta]`, unitless | Angular QE divided by the supplied normal-incidence QE; exactly 1 at theta 0 |
| `full_well_e` | electron/pixel | Physical well clipping capacity |
| `read_noise_e` | RMS electron | Input-referred Gaussian read-noise standard deviation |
| `dark_current_e_per_s` | electron/pixel/s | Mean dark-current rate; shot noise is included when noise is enabled |
| `gain` | unitless | Physical amplification, not the legacy inverse-gain convention |
| `black_level_dn` | DN | Digital black pedestal, below the ADC white level |

No additional CFA transmission, fill factor, lens transmission, cosine factor,
or solid-angle Jacobian is multiplied into these contracts. Multiplying those
effects again would double count them. Normalized angular weights must sum to
one for each field; the runtime refuses unnormalized weights. Sensor angular QE
cannot exceed one collected electron per incident photon in this linear pilot.

All common spectral arrays share exactly the supplied wavelength coordinates.
The runtime does not infer spectra from RGB or lux, extrapolate angular
responses, or perform implicit spectral resampling. Angular interpolation stays
within the supplied sensor domain. Lens angular weights and relative
illumination are wavelength-independent in this version.

The Quick Estimate integrates:

```text
expected photoelectrons = pixel_area_m2 * exposure_s
    * integral(irradiance_W_m2_nm * effective_QE * wavelength_m / (h*c), d wavelength_nm)
```

The integration variable remains nm because irradiance is specified per nm;
the photon energy separately uses wavelength in metres. Trapezoidal integration
is used on the declared samples. `h = 6.62607015e-34 J s` and
`c = 299792458 m/s` are exact SI constants. Response-DB first combines the lens
angular power fractions with interpolated sensor angular response, then
performs the same spectral integral and applies relative illumination.

Temperature is recorded as a condition. There is no temperature model in this
adapter; the selected QE and readout parameters must already apply to it.

## Result contract

Both modes provide `summary.channel_electrons` in `[field][R,G,B]` order,
`field_radius`, `channel_reference_signal_dn`, `channel_ratios`,
`theoretical_snr_db`, `saturation_margin_e`, conditions, units, and limitations.
Quick Estimate has one field at radius zero. The complete summary serializes
with `json.dumps(summary, allow_nan=False)`.

The prediction used by validation has this shape:

```json
{
  "metric_id": "raw.channel_ratio",
  "unit": "1",
  "values": [0.6, 0.4, 0.45, 0.36, 0.3, 0.32],
  "axes": [
    {"name": "field_radius", "unit": "1", "values": [0.0, 0.5, 1.0]},
    {"name": "channel_ratio", "unit": "1", "values": ["R/G", "B/G"]}
  ],
  "reference_plane": "sensor_raw",
  "source_kind": "simulated",
  "method_and_version": "response_db_v1:continuous_dark_corrected_expectation",
  "uncertainty": {"kind": "unknown"}
}
```

The real object also contains `conditions` and `included_effects`. Values flatten
field-major, R/G then B/G. Ratios use continuous expected RAW after clipping and
matched dark subtraction; they do not incorporate ADC rounding or the sampling
uncertainty of measured frame averages. A zero green denominator produces
`null`, never a fabricated ratio. Model uncertainty remains unknown, so a
matching curve alone cannot establish physical validation.

For a registered RAW observation Test, `camera_platform.observations` additionally
applies the frozen native/crop CFA and ROI sampling operator to these continuous
channel signals. It averages channels before forming R/G and B/G and matches the
declared black-only or matched-dark correction. The service replaces `prediction`
with that ROI observation and stores the weighting/means in `observed_measurement`;
the original field curves and generated preview raster remain separately labeled.
Quick Estimate has no spatial basis and cannot execute an observation Test.
See [RAW observation contract](camera-platform-measurement.md). This adapter does
not turn the generated representative raster into a native capture, nor assign a
model uncertainty bound from agreement with the uploaded frames.

| Artifact key | Shape | Meaning |
| --- | --- | --- |
| `field_channel_electrons` | `[field,3]` | Spectral/angular-integrated native-pixel charge |
| `mean_electrons` | `[rows,cols]` | Expected photoelectrons at each generated RGGB site |
| `radius_map` | `[rows,cols]` | Normalized field coordinate; corner equals 1 |
| `raw_frames` | `[frames,rows,cols]` | Integer DN, including digital black pedestal |
| `rgb` | `[rows,cols,3]` | First RAW frame after bilinear demosaic and sRGB transfer |
| `raw_temporal_variance_dn2` | `[rows,cols]` | Unbiased temporal variance across repeated frames; omitted for one frame |

Quick Estimate emits only `field_channel_electrons`. Response-DB uses the
public `configure_e2e_sensor`, `resample_sensor_capture`, and `ip_compute` entry
points. Frame seeds are `seed + frame_index`; the same request reproduces the
same arrays in the same recorded executable environment.

`output_size` is a representative raster of the complete normalized field.
Each sample carries one native pixel's expected charge; it is **not a contiguous
native-resolution capture**. For example, a 64×64 raster does not replace a
1920×1440 sensor with a 64×64 native sensor. Neighbor interpolation in the RGB
preview is for inspecting the field pattern. Resolution, MTF, image-scale and
scene-geometry conclusions are outside this representation.

RGB uses unity channel gains, identity color correction, no lens shading
correction, and no frame-maximum normalization. It is sensor RGB with an sRGB
transfer function, **not calibrated colorimetric sRGB**. RAW ratios retain the
primary evidence for the color-shading pilot.

`raw_temporal_noise_dn` is the square root of mean per-pixel temporal variance.
`raw_temporal_snr_db` uses expected photo-signal divided by that temporal noise.
A single-frame spatial standard deviation is never used as temporal noise.
With one frame, no temporal SNR is reported. With clipping above 0.1% at either
well/ADC boundary, the temporal SNR result is withheld. The idealized
`theoretical_snr_db` likewise excludes saturated conditions and does not claim
EMVA-standard compliance.

## Bounds and unsupported effects

Response-DB allows even raster sides from 8 to 512, 1–16 frames, and at most
4,194,304 frame-pixels. Spectral axes contain 2–2048 samples in 200–2500 nm;
angular axes contain 2–181 samples in 0–89.9 degrees. Field axes contain 2–256
samples and cover 0 through 1. Excessive charge is refused before sampling.

This adapter does not model PSF/OTF, distortion, general scenes, polarization,
azimuth, crosstalk, PRNU, DSNU, moving rolling shutter, HDR, auto exposure, or
closed-loop control. Engine availability does not imply that these features
are connected through this adapter. External characterization solvers require
their own contracts and independently checked response data.

## Verification

```sh
python -m pytest tests/platform/test_runtime.py tests/platform/test_runtime_integration.py -q
```

The tests check the spectral integral against an analytic solution, physical
exposure/area scaling, angular ratios, quantized RAW and actual RGB changes,
seed reproducibility, independent frame noise, expected Poisson/read-noise
variance, black pedestal, clipping, service model translation, and persisted
artifacts. Test inputs are labeled analytic fixtures. These checks establish
implementation evidence; physical validation still requires independent
measurements, matched conditions, known measurement uncertainty and an adequate
prediction-uncertainty model.
