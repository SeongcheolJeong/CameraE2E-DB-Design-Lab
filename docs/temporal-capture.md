# Temporal capture: motion, rolling shutter and exposure

The Python camera pipeline accepts a continuous scene provider, `scene_at(t)`.
It evaluates spectral optical images over each sensor row's exposure interval,
integrates the noise-free sensor response, and then performs one sensor readout
and ISP evaluation per output frame. This produces exposure blur, rolling-shutter
skew, and time-varying illumination effects in the actual sensor arrays.

## Timing and units

`TemporalCaptureConfig` uses **seconds**, including `frame_start_s` and
`line_time_s`. The sensor's scalar `integration_time` is also in seconds.
`scene_at(t)` and the lower-level `oi_at(t)` receive absolute simulation seconds.
The HW ISP API continues to use **microseconds** for `exposure_time_us`,
`line_time_us` and `exposure_times`.

For active row `r`, rolling shutter integrates over:

```text
start(r) = frame_start_s + (hidden_lines_top + r) * line_time_s
end(r)   = start(r) + integration_time
```

Global shutter uses the same exposure interval for every active row. Serial
readout can still take time after a simultaneous global exposure. Changing the
shutter mode therefore changes optical integration timing; readout latency and
exposure timing are separate quantities. For global shutter the common start is
`frame_start_s`; hidden row counts do not delay this common optical exposure.

## One moving-scene capture

```python
import math

from pyisetcam import (
    TemporalCaptureConfig, camera_compute_temporal, camera_create,
    configure_e2e_sensor, scene_create, scene_set, sensor_set,
)

camera = camera_create()
sensor = sensor_set(camera.fields["sensor"], "auto exposure", False)
sensor = sensor_set(sensor, "integration time", 0.008)  # 8 ms
camera.fields["sensor"] = configure_e2e_sensor(sensor)
base_scene = scene_create("uniform ee", 32)
base_photons = base_scene.data["photons"].copy()

def scene_at(t_s):
    # Replace this analytic illumination with a moving scene or renderer output.
    scale = 1.0 + 0.5 * math.sin(2.0 * math.pi * 120 * t_s)
    return scene_set(base_scene.clone(), "photons", base_photons * scale)

frame = camera_compute_temporal(
    camera, scene_at,
    timing=TemporalCaptureConfig(
        frame_start_s=0.0,
        line_time_s=20e-6,
        rolling_shutter=True,
        hidden_lines_top=0,
        samples_per_exposure=16,
        max_scene_samples=4096,
    ),
    seed=2026,
)
raw_dn = frame.fields["sensor"].data["raw_dn"]
mean_electrons = frame.fields["sensor"].data["mean_electrons"]
row_timing = frame.fields["sensor"].metadata["temporal_capture"]
```

The callback should return a `Scene` with consistent image geometry, spectral
sampling and field of view. It should depend on simulation time, not callback
invocation count: shared sample times may be evaluated once and reused. With
`sensor_resize=False`, sensor dimensions are kept fixed; otherwise the normal
camera FOV matching is applied. To supply already computed spectral irradiance,
use `sensor_compute_temporal(sensor, oi_at, timing=..., seed=...)`.
Both temporal entry points produce the digital RAW capture contract. Disable
low-level automatic exposure and set a scalar exposure before calling them;
the HW ISP path resolves its AE controller before each temporal capture.
The camera's retained optical image is a labeled representative midpoint
snapshot; it is not the integrated RAW exposure.

## Multi-frame HW ISP and CameraE2E

```python
from pyisetcam import hw_isp_config, hw_isp_simulate_sequence

config = hw_isp_config(
    fps=30,
    exposure_time_us=8000,
    line_time_us=20,
    rolling_shutter=True,
    temporal_samples_per_exposure=16,
    temporal_max_scene_samples=4096,
    control_path={"apply_to_image": True},
    seed=2026,
)
sequence = hw_isp_simulate_sequence(
    camera, scene_at, config,
    nframes=3,
    exposure_times=[8000, 8000, 8000],  # microseconds
)
raw_frames = [frame.sensor.data["raw_dn"] for frame in sequence.frames]
```

For a callable provider, `nframes` defines the finite sequence length. Frame
timestamps remain continuous, and the applied exposure from the HW control path
defines the integration window. Independent frame seeds provide reproducible
temporal noise. An ordinary `Scene` or list of scenes retains the static-frame
behavior: an image list alone does not define motion between its entries.
The basic `camera_compute_sequence()` also uses the supplied or stored base noise
seed plus the frame index, while retaining the sensor's fixed-pattern maps.
With image-applied AE enabled, the first requested exposure initializes the
controller and later frame exposures are controller outputs. To execute the
explicit exposure list unchanged, set `control_path={"ae_enabled": False}`.

CameraE2E scenarios can also carry a callable scene provider and must enable
`hw_isp.enabled=True`. Their sequence
contains each captured frame, while the final frame remains the representative
image for the existing single-image report. Dataset export can opt into
`export_sequence=True` to save the full sequence. A Python callback is executable
input; save its parameters and source with a dataset if exact replay is required.
Dataset splits keep frames from each scenario together. Supplied labels are
copied across frames; motion does not automatically update bounding boxes or
other spatial annotations.

```python
from pyisetcam import camerae2e_dataset_export, camerae2e_run_scenario

scenario = {"name": "moving_scene", "hw_isp": {"enabled": True, "nframes": 3}}
result = camerae2e_run_scenario(scenario, scene=scene_at, camera=camera, seed=2026)
manifest = camerae2e_dataset_export(
    "outputs/temporal_sequence", [scenario],
    scene=scene_at, camera=camera, seed=2026, export_sequence=True,
)
```

## Physical order and current limits

At each temporal sample the complete spectral optics and sensor spatial/QE
response are evaluated. The integration combines **noise-free response / mean
photoelectrons**, before noise, full-well clipping, gain and ADC quantization.
Dark charge is added for the row's actual exposure duration. Shot noise, fixed
pattern response, well clipping, read noise and digital readout are then applied
once per frame using the normal sensor capture chain. The ISP consumes that RAW.
Averaging already noisy or clipped RAW captures would have different statistics
and is not the implementation used here.

This is numerical temporal integration, not a new 3D renderer. The callback can
provide an external renderer's scenes, including object/camera motion, occlusion
and changing illumination. If the provider merely translates an image or
interpolates sparse rendered frames, the result inherits that approximation;
interpolation does not reconstruct missing visibility or lighting events.

`samples_per_exposure` controls temporal resolution: shared sample nodes span
the earliest row start through the last row end, with spacing no greater than
`exposure / samples_per_exposure`. Adjacent sampled sensor responses are linearly
interpolated, and the piecewise-linear response is integrated exactly over each
row's window, including partial intervals at its boundaries. Rapid motion, short pulses
and PWM transitions may require more samples. Increase it and compare RAW/charge
and the relevant metrics until they converge. The requested accuracy is not an
automatic error guarantee. `max_scene_samples` imposes a hard computational cap:
an over-budget request raises an error instead of silently reducing temporal
resolution. Short exposures combined with a long row scan can require many
distinct sample times, and every sample processes a full spectral sensor input.
Start with small images and a limited spectral grid when exploring timing.

The temporal path supports one scalar, linear exposure per frame. HDR bracketing,
staggered HDR, nonlinear in-exposure sensor response, 3D reconstruction, optical
flow and temporal denoising/fusion are separate features. Existing HW TNR stage
latency does not imply a temporal denoising image algorithm.

## Reproducible visual demonstration

```bash
python tools/render_temporal_capture_demo.py --output-dir reports/temporal_capture
```

The demo sends a moving bright vertical bar through the same stationary spectral
camera for four cases: global/rolling shutter crossed with 2 ms/16 ms exposures.
It disables noise and dark current to isolate geometric and exposure effects,
uses 1 ms per sensor row, and keeps one fixed 0–1000 DN display scale across all
four images. The 12-bit RAW arrays themselves are not normalized or reduced to
the PNG display range.

The output directory contains:

- `temporal_capture_raw.npz`: physical `raw_dn`, `reference_dn` and
  `mean_electrons` arrays for each case.
- `temporal_capture_montage.png`: four RAW views on the same DN scale.
- `temporal_capture_metrics.json`: capture parameters, row centroids, fitted
  full-height centroid skew, and within-row blur width measured as the spatial
  standard deviation of the mean-photoelectron distribution.

The width metric includes the finite bar width, spatial optics and sampling as
well as exposure motion. Comparing short/long cases isolates the added temporal
spread. Global captures should have near-zero row skew, rolling captures should
show nonzero skew, and long exposures should increase within-row spread.

For the supplied scene and the default 16 samples/exposure, the checked run
produced:

| Capture | Fitted row skew | Mean within-row sigma |
| --- | ---: | ---: |
| Global, 2 ms | approximately 0 px | 1.655 px |
| Global, 16 ms | approximately 0 px | 2.835 px |
| Rolling, 2 ms | 15.481 px | 1.742 px |
| Rolling, 16 ms | 15.491 px | 2.848 px |

The analytic row displacement for the bar is
`500 px/s * 0.001 s/row * 31 rows = 15.5 px`; small differences include the
camera's spatial response. A separate 16-to-32 sample check of both 16 ms
captures changed the mean-charge arrays by less than `2e-8` relative L2 norm.
That convergence result is specific to this smooth, simple motion and does not
establish accuracy for a different scene or a pulsed light source.
