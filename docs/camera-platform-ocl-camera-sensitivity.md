# OCL response hypothesis → native RAW and ISP

This is an assumption-driven sensitivity demonstration, **not a qualified OCL
camera response**. The existing `sensor_structure_response` gate continues to
reject 2D sources for camera QE. No new Meep solve or extension of the eight-hour
budget is involved.

## Bound evidence and camera

The completed trend run `run-51b6c8b3a54a4bc8af0ab7c1fddb1315` supplies stable
unpolarized central-absorption ratios at 550 nm, 0° and +20°, from an IMX662
all-green 2D cross-section and a finite Gaussian beam. The OCL shift is +0.1
pitch (+0.29 µm). The ratios are approximately 0.98745 and 1.06505. Total active
Si absorption changes are below the configured 1% meaningful threshold: this is
principally evidence of redistribution, **not uniform-field QE improvement**.

The host is saved Canon p0014 × IMX662 candidate-01 of
`run-33817fe3ce14497d88aa4ef55e0b4cda`. Native 256² optical mean-electron-rate
arrays, model, calibration and source manifest are hash-bound. Its geometric
optics, planar spectral model, charge-collection assumptions and ASI662MC readout
proxy remain unchanged. The outer-ne probe has CRA 18.7213° and azimuth45°;
its virtual image-plane position is outside the physical IMX662 die. It is not
a supported full-sensor mode.

## Explicit projection assumptions

- The finite-beam **central** absorption ratio is assumed to multiply the local
  scene electron rate. It is not a derived general spatial response. The seven
  absorption bins are never used as an image convolution kernel.
- `green_only` applies it to Gr/Gb over the host Green band, leaving R/B fixed.
  `achromatic_shared` applies the same ratio to all channels. These are two
  hypotheses, not uncertainty bounds or measured spectral predictions.
- Center/outer-ne use the exact 0°/+20° evidence nodes. This is a declared angle
  mapping, not interpolation, lens-pupil integration or azimuth qualification.
- Fixed 4ms, gain1, 25°C,8 independently reset frames, D65 1000cd/m² ×0.1/1.
  Shot/read noise, well/ADC clipping and integer RAW are calculated afresh.
  A/B uses paired simulation seeds; persistent FPN is unchanged (zero in the
  saved model). No previously noisy RAW is scaled into a new acquisition.
- Bilinear ISP, common source electron→RGB matrix, WB1, LSC OFF, NR/sharpen OFF,
  fixed white1 and gamma2.2. The common matrix originated in the IMX462 Reference
  calibration and is **not** independently fitted/measured IMX662 calibration.

## Outputs and metrics

32 stored cases cover two fields, four charts, two illuminance scales and two
hypotheses. RAW previews use common full-well electron scaling. Signal is
available as expected photoelectrons and observed mean electrons. Noise uses
per-pixel temporal variance with ddof1; image texture is not noise. Absolute
noise can rise while SNR improves. A/B validity masks are shared; saturated
pixels remain in saturation denominators. Uniform charts are the appropriate
homogeneous SNR comparison.

Color ΔE00 is **A/B appearance shift**, not accuracy against a measured target.
The main diagnostic uses noiseless expected electrons; acquired repeat-average
values are retained separately. Both use the same declared D65 white scaled by
input light, not per-image normalization. The signed difference preview is
0.5 +5×(changed−baseline) in noiseless linear RGB, with a fixed display scale.

Original source files remain unchanged. New native RAW, repeated electron and
linear RGB arrays, means, variance, invalid/saturation masks, configuration,
source hashes and metrics are retained on the SSD under the existing500GB
project limit. No physical-validation or camera-QE eligibility flag is promoted.

## Run and reopen

```python
from camera_platform.sdk import PlatformClient
with PlatformClient("http://127.0.0.1:8785", timeout=120) as client:
    spec = client.sensor_structure_camera_template()["specification"]
    job = client.submit_sensor_structure_camera_study(spec)
    result = client.wait(job["id"], timeout=1800,
                         expected_kind="sensor_structure_camera_demo")
```

Or run `python tools/run_sensor_structure_camera_demo.py`. Typed routes are
`/api/v1/sensor-structure-camera-studies` with template/latest/detail/cancel/files.
Common job wait returns typed results. Open
`/sensor-structure-camera.html?run=<id>`. Changing viewing controls navigates
saved cases only; it never pretends that a new simulation has completed.

## Prepared demo — 2026-10-18

Completed run: `run-cce21475c7ee43dead6f77bb41f79eff`.
Open [outer-ne color A/B](http://127.0.0.1:8785/sensor-structure-camera.html?run=run-cce21475c7ee43dead6f77bb41f79eff&field=outer-ne&chart=color&light=1&profile=green_only&stage=isp).
The 32 cases took 360.5 seconds in the recorded execution and retained
476,963,908 bytes. All 357 manifest file hashes and 16 common baseline RAW pairs
were checked; the API RAW download matched its manifest hash. Native RAW is
8×256×256 integer ADC output, with 12-bit bounds. The focused suite passed 34 tests.
Delivery and browser evidence are in `outputs/ocl-camera-demo-20261018/completed/`.

Under the Green-only local-gain hypothesis at unit light, center Gr/Gb signal
changes −1.255%, temporal noise −0.38/−0.57%, and uniform-chart SNR −0.86/−0.68%.
At the virtual outer-ne probe, signal changes +6.505%, noise +3.02/+3.00%, and
SNR +3.38/+3.42%. Color-chart appearance shift is 0.863/4.166 ΔE00 respectively.
These camera changes follow the projection hypothesis; they do not qualify the
finite-beam redistribution result as a uniform-field QE or physical camera model.
