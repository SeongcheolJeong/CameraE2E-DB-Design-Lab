# Comparing image sensors from DB estimates

This guide describes the reproducible historical DB comparison. The default
single-capture configuration now selects the [fast physical estimate](physical-sensor-e2e.md)
when canonical Bayer geometry is available. `camerae2e_compare_sensors` explicitly
retains `db_estimate_v1`; use `camerae2e_compare_physical_sensors` for the new comparison.

`camerae2e_compare_sensors` makes the existing catalog usable without claiming
that missing electrical parameters were measured. It uses the `db_estimate_v1`
optical model, explicitly assumed readout parameters, integer digital RAW, and a
fixed linear ISP. The default representative sensors are IMX307
(`DEF-1807-801`), IMX462 (`DEF-2011-804`) and IMX662 (`DEF-2206-805`). Differences
in packaged optical stack/CFA/silicon estimates can therefore affect results;
shared readout assumptions do not imply equal measured read noise or full well.
For individual captures, output units and migration, see the
[digital RAW API guide](sensor-capture-api.md).
For explicit CFA/microlens FDTD spectral and angular response, validation gates,
and execution commands, see the [FDTD CFA guide](fdtd-cfa-e2e.md).

```python
from pyisetcam import camerae2e_compare_sensors

report = camerae2e_compare_sensors(output_dir="reports/db_sensor_comparison")
```

A bounded smoke run is useful before the full experiment:

```sh
PYTHONPATH=src python tools/compare_sensors.py \
  --sensors DEF-1807-801 DEF-2011-804 \
  --frames 4 --luminances 10 --exposures .004 \
  --output-size 32 32 --scenes uniform --baseline-only \
  --output-dir reports/db_sensor_smoke
```

The full defaults use three scenes (uniform D65, a D65 Macbeth chart, and a
static slanted edge), three luminances (1, 10, 100 cd/m²), three exposures
(1, 4, 16 ms), 32 temporal frames, and 128 × 128 output pixels. With the three
representative sensors this means 891 reported cases and 28,512 temporal
samples. Only nine optical simulations are needed: photoelectron means are
cached once per sensor and scene at 1 cd/m² and 1 ms, then scaled in proportion
to luminance, exposure and effective efficiency before any well/ADC clipping.
Noise is independently sampled for each frame; deterministic seeds permit
replay. These linear scaling assumptions are specific to this static, linear
single-exposure model.

## Matched comparison contract

The focal length (4 mm) and f-number (f/4) are fixed. A centered common physical
crop is defined as the output dimensions times the largest selected pixel
pitch. Each sensor uses enough native pixels to cover that same physical crop,
with a small border for interpolation. The native CFA origin is preserved.
After demosaicing, Gaussian antialias filtering and bilinear resampling map the
physical crop to common output pixels. The JSON records the physical extent,
FOV, native sizes and output pitch. This is an explicitly modeled crop, not a
claim that the catalog contains verified full-die dimensions. A larger pixel
therefore collects more photons natively; resampling determines whether this
advantage survives at matched output scale.

The RAW path includes photo and dark shot noise, configured fixed patterns,
full-well clipping, read noise, physical analog amplification, black pedestal
and ADC rounding/clipping. Default gain is 1, bit depth is 12, and black level
is 64 DN. FWC, ADC-white and ADC-zero saturation are reported separately.
Uncertain readout parameters below are assumed equally across sensors.

The common-output path uses the actual CameraE2E signed `(RAW − black) /
(white − black)` normalization, bilinear demosaic, and fixed D65 sensor/XYZ/
display matrices. The implementation caches those matrices and verifies its
fast transform against `ip_compute`; it applies the same internal/display
nonnegative clipping. The matrices include the model's fixed D65 white
calibration, which gives useful linear output levels without fitting each frame.
There is no frame-max brightness normalization, automatic
exposure or frame-dependent white balance in the fixed comparison. HTML view
exposure is chosen once per optical condition and shared across sensors; it is
not used for numerical metrics.

## What the metrics mean

- Mean photoelectrons are photo-signal only, before dark, noise, gain and clipping.
- Native RAW and common-output SNR use temporal variance across repeated frames
  at each pixel. Spatial scene contrast is not a noise estimate. Common-output
  noise includes the effects of DN normalization, demosaicing and resampling.
- Common-output signal subtracts the noiseless dark reference from the noiseless
  illuminated reference. The RGB luminance weights are 0.213, 0.715 and 0.072.
- Macbeth color error is an **in-sample calibration fit residual**, expressed
  as CIE76 over the inner regions of 24 patches with white-patch Y normalization.
  `sensor_to_target_matrix` fits the sensor-to-XYZ matrix by least squares using
  the 24 reflectances in `macbethChart.mat` under D65. The evaluation scene and
  ideal patch values reuse those same reflectances under D65. The residual also
  includes optical mixing, capture noise and ISP effects. It is not independent
  color accuracy: there are no held-out surfaces or illuminants, and it does not
  establish generalization to other scenes. JSON `method.color_evaluation`
  records both calibration and evaluation provenance; each color metric points
  to this contract.
- Slanted-edge MTF50 uses the existing ISO 12233 estimator on the temporal mean
  image, expressed in cycles per output pixel. The first half-contrast crossing
  is interpolated on its luminance MTF curve below Nyquist; the inherited
  estimator's coarsely quantized scalar MTF50 is not used. Invalid/too-small
  estimates are explicitly marked unavailable.

## Assumption sensitivity

The eleven sets are one baseline, eight one-at-a-time low/high changes, and two
corners. They are sensitivity bounds, not statistical confidence intervals.

| Parameter | Baseline | Low | High |
| --- | ---: | ---: | ---: |
| Effective optical/collection efficiency | 0.75 | 0.5 | 1.0 |
| Full well, electrons | 10,000 | 5,000 | 20,000 |
| Read noise, electrons RMS | 10 | 3 | 20 |
| Dark current, electrons/pixel/s | 10 | 1 | 100 |

The optimistic corner uses efficiency 1, full well 20,000, read noise 3 and dark
current 1; the pessimistic corner uses 0.5, 5,000, 20 and 100. Assumptions are
shared across the selected sensors. The report gives SNR ranges and whether
ordering changes across those shared assumptions. This does not establish a
measured product ranking: each real sensor may occupy a different point in the
assumption ranges. ADC clipping can also make SNR alone a poor quality measure;
inspect the separate saturation metrics and images. Cases with more than 0.1%
native pixels at a well/ADC bound are excluded from SNR ordering. An assumption
set missing an unsaturated selected sensor is excluded from order-change
comparisons, so a missing sensor cannot create an artificial rank reversal.

## Artifacts and coverage

`comparison.json` contains all numerical cases, assumptions, model provenance,
comparison geometry, uncertainty summaries and the full catalog modelability
inventory, including reasons for exclusions. `comparison_arrays.npz` stores
baseline conditions only: one representative frame, noiseless reference,
temporal mean/variance maps and native photoelectron/RAW arrays. It does not
store all 32 frames or repeat arrays for every sensitivity set. `index.html`
provides shared-scale image/noise views and a filterable metric table.
Stored RAW frame codes remain `int32` DN; temporal means and variances remain
`float32` (DN and DN² respectively). JSON records the array storage dtypes.
It also plots SNR against exposure with assumption-range shading, plus Macbeth
in-sample CIE76 residual and slanted-edge curves. Plot values come from the same
JSON metrics.

The inventory includes all catalog records, while expensive simulation runs
only the selected records. Unknown/unmodelable CFA or missing pitch/optical
information is an explicit exclusion. The implementation does not fabricate
full resolution, shutter mode, FPS, HDR behavior, motion effects, measured QE,
FWC, gain calibration, read noise or dark current. The configured static
comparison has AE disabled; a separate AE smoke test must not be interpreted as
a fixed-control sensor comparison or as rolling-shutter validation.
