# CameraE2E digital RAW contract

CameraE2E scenarios and HW ISP frames default to `sensor_output_mode="digital_raw"`.
The DB optical estimate and the readout assumptions are separate inputs. This
mode supports one static, linear, visible-light mosaic exposure. HDR, binning
and logarithmic sensor modes require the explicit legacy path.

## Run a DB sensor

`image_sensor_db_config` now uses `model="auto"`: canonical Bayer records use
the [fast physical model and readout profiles](physical-sensor-e2e.md), with an
explicit fallback reason for other DB-eligible records. Use `model="db_estimate_v1"`
to reproduce the older aggregate-loss model. The readout values below are explicit
example assumptions and override any selected operating-point profile.

```python
from pyisetcam import camera_get, camerae2e_run_scenario, image_sensor_db_config

scenario = image_sensor_db_config("DEF-2011-804")["scenario"]
result = camerae2e_run_scenario({
    **scenario,
    "scene": {"type": "uniform ee", "args": [32]},
    "sensor": {"integration_time": 0.004, "noise_flag": 2},
    "readout": {
        "gain": 2.0,
        "full_well_e": 10000,
        "read_noise_e": 10,
        "dark_current_e_per_s": 10,
        "bit_depth": 12,
        "black_level_dn": 64,
    },
}, seed=17)
sensor = camera_get(result["camera"], "sensor")
raw = sensor.data["raw_dn"]
mean_photocharge = sensor.data["mean_electrons"]
capture_metadata = result["sensor_capture"]
model_provenance = result["sensor_estimate"]
```

The example uses the scene factory's illuminant. The comparison runner explicitly
sets D65 and scene luminance; see [DB sensor comparison](db-sensor-e2e.md).
Readout settings are assumptions, not measured specifications of the named sensor.
Explicit `readout` entries override sensor settings and model defaults. For DB
optical assumptions, use `image_sensor_db_config(..., assumptions={...})`.

## Outputs and processing order

| Output | Meaning |
| --- | --- |
| `mean_electrons` | Mean photoelectrons/pixel before dark charge, fixed patterns, noise, gain or clipping |
| `reference_dn` | Continuous deterministic code before ADC rounding; includes dark charge, enabled fixed patterns and clipping |
| `raw_dn` / `dv` | Integer ADC output including black pedestal |
| `volts` | Signed amplified analogue signal relative to the digital black pedestal |
| `sensor_capture` metadata | Exposure, seeds, readout assumptions, conversion scale, units and separate well/ADC saturation fractions |

`reference_dn` is the deterministic reference chain, not the statistical mean
of the noisy output near a nonlinear clipping boundary. Use repeated captures
to estimate the latter.

Photo and dark shot noise are followed by PRNU/column response gain,
full-well clipping, signed fixed pixel/column bias offsets, signed read noise,
physical gain, digital black pedestal,
and ADC rounding/clipping. Gain 2 doubles an unclipped reference code above
black while leaving `mean_electrons` unchanged. Signed fixed bias is applied after
physical charge clipping so it remains signed at darkness. Generic defaults are
gain 1, FWC 10,000 e,
read noise 10 e RMS, dark current 10 e/pixel/s, ADC 12 bit, black 64 DN and white
4095 DN. The new physical path uses its selected profile instead of these generic
capacity/noise defaults. The model assumes conversion gain from the configured well and voltage
swing; it does not emulate a measured readout circuit.

The ISP consumes `(raw_dn - black) / (white - black)`. Black is subtracted once,
and signed values reach demosaicing. D65 reference matrices supply a fixed
brightness scale. There is no per-frame maximum normalization. ADC bit depth,
gain and saturation therefore affect the final image. General/Quad CFA layouts
use a normalized masked interpolation that preserves measured samples and flat
fields; ordinary Bayer retains its bilinear path.

`sensor_get(sensor, "electrons")` retains its historical voltage-derived
meaning. Read `sensor.data["mean_electrons"]` for the new photocharge contract.

## Low-level use and repeated captures

```python
from pyisetcam import (
    apply_sensor_estimate, configure_e2e_sensor, image_sensor_db_model,
    resample_sensor_capture, sensor_compute, sensor_create,
)

sensor = apply_sensor_estimate(sensor_create(), image_sensor_db_model("DEF-2011-804"))
sensor = configure_e2e_sensor(sensor, readout=sensor.fields["e2e_model_readout"])
# oi is an existing computed optical image.
first = sensor_compute(sensor, oi, seed=101)
second = resample_sensor_capture(first, seed=102)
```

`resample_sensor_capture` reuses the stored optical mean. It is appropriate for
repeated frames of an unchanged static input. If scene, optics, exposure or QE
changes, recompute the optical mean with `sensor_compute`. Applying a different
DB model invalidates cached captures. Use `configure_e2e_sensor(..., readout=...)`
to change marked sensor gain or readout settings; the legacy analog-gain field
stores a reciprocal compatibility value.

## Explicit compatibility

Unmarked low-level `sensor_compute` retains the existing ISET-style behavior.
To use that behavior through CameraE2E, select it explicitly:

```python
legacy_scenario = image_sensor_db_config("DEF-2011-804", model="legacy")["scenario"]
legacy_result = camerae2e_run_scenario(
    legacy_scenario, sensor_output_mode="legacy", seed=17,
)
```

For HW ISP, pass `sensor_output_mode="legacy"` to `hw_isp_simulate_frame` or
`hw_isp_simulate_sequence`. Digital `readout` settings cannot be combined with
legacy mode. Legacy FDTD/TCAD proxy behavior and earlier numerical corrections
are described in [sensor model validation](sensor-model-validation.md).

## Reproduce the delivered evidence

```sh
PYTHONPATH=src python tools/compare_sensors.py --output-dir reports/db_sensor_e2e/comparison
PYTHONPATH=src python tools/verify_sensor_e2e.py --output reports/db_sensor_e2e/verification
```

The first command generates fixed-control comparisons. The second separately
checks RAW/ISP coupling, physical gain and static AE convergence. These establish
software and model consistency; measured calibration remains unverified.
