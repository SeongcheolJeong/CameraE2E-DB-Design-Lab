# Practical sensor performance estimates

CameraE2E can now run a fast visible-band sensor model without a validated 3D
FDTD LUT. It connects the existing material and geometry database to spectral
photon conversion, an explicit angular illumination distribution, operating-point
readout, integer RAW and ISP output. This is suitable for controlled camera
performance scenarios and sensitivity studies. Its results remain estimates;
they are not calibrated product specifications or a pixel-design prediction.

## Model selection and compatibility

`image_sensor_db_config(sensor_id_or_code, model="auto")` is the default.

- Eligible native RGB Bayer records with canonical CFA materials and optical
  geometry use `physical_estimate_v1`.
- Other DB-eligible records retain `db_estimate_v1`, with the reason recorded in
  `config["model_selection"]` and the scenario's `image_sensor_db.model_selection`.
  Examples include an unsupported CFA layout or missing canonical geometry.
- DB records already excluded from optical estimation still raise an error.
  A fallback does not make missing physical inputs available.
- Explicit physical-only options are rejected if automatic selection falls back
  to the old DB model. They are never silently ignored.

Select `model="physical_estimate_v1"` to require the fast model explicitly, or
`model="db_estimate_v1"` to reproduce the previous DB estimate. Supplying legacy
DB optical assumptions such as `effective_efficiency` or
`active_si_thickness_um` with `model="auto"` also preserves the DB path and records
that reason. The original analytic/FDTD/TCAD configuration remains explicitly
available as `model="legacy"`; it is separate from both estimate models.

The fast model accepts `hybrid` or `analytic_only` strategy, and does not attach
old FDTD/TCAD multiplier layers. It rejects a LUT path, `lut_only`, and requests
to attach shared or verified sensor-specific physics artifacts. The default
normal-incidence illumination is an explicit recorded assumption when the user
does not provide sensor illumination.

## Quick 8-pixel example

Run in the configured CameraE2E Python environment with the repository's normal
asset database available:

```python
from pyisetcam import camera_get, camerae2e_run_scenario, image_sensor_db_config
from pyisetcam.sensor_fdtd_comparison import sensor_pupil_directions

# This describes a local field patch: CRA 20 degrees, azimuth 0, image-side F/4.
illumination = sensor_pupil_directions(20.0, 0.0, 4.0)
config = image_sensor_db_config(
    "DEF-2011-804",  # IMX462; canonical DB ID or report code
    sensor_illumination=illumination,
    operating_point="low_gain",
)
result = camerae2e_run_scenario(
    {
        **config["scenario"],
        "scene": {"type": "uniform ee", "args": [8]},
        "sensor": {"size": [8, 8], "integration_time": 0.004, "noise_flag": 2},
        "sensor_resize": False,
        "optics": {"f_number": 4.0},
    },
    seed=7,
)
sensor = camera_get(result["camera"], "sensor")
print(config["model_selection"])
print(sensor.data["mean_electrons"].shape)  # (8, 8), before dark/FPN/readout
print(sensor.data["raw_dn"].dtype)         # int32, includes digital black pedestal
print(sensor.metadata["sensor_readout_profile"]["profile"])
```

Setting a scalar `integration_time` disables automatic exposure. This example
uses the standard uniform equal-energy scene; it is a wiring example, not a
specified 100 cd/m² test. The comparison tool below controls scene luminance.
`sensor_resize=False` preserves the requested sample grid. A changed optical
model must recompute photocharge; readout-only scenarios can resample the cached
mean charge using `resample_sensor_capture`.

## Optical and electrical quantities

The fast stack uses phase-averaged Fresnel power transport, Snell refraction and
Beer-Lambert absorption, including repeated internal intensity reflections.
CFA `n,k` come from the canonical material bundle. Absorption coefficient is
`alpha = 4*pi*k/lambda`, and oblique propagation uses the increased path through
each layer. Lens, CFA, passivation and active-Si absorption remain separate.

Fresnel boundaries currently use real refractive index; the imaginary component
enters propagation loss. This is a documented approximation rather than a full
complex-index boundary calculation. The Si beyond the active depth continues
without a fabricated Si/air interface. Light absorbed deeper is uncollected in
this model, and no invented backside reflection creates spectral fringes.

The nominal response is

`QE_c(lambda, theta) = active_Si_absorption_c(lambda, theta) * collection_efficiency`.

`collection_efficiency` defaults to 0.75 and means collected carriers per photon
already absorbed in active Si. It has the same meaning in the new FDTD model.
It is a scalar assumption, with 0.5/0.75/1 sensitivity cases; it is not inferred
from TCAD or fitted to measured QE. Do not compare this coefficient directly
with the old DB model's 0.75 `effective_efficiency`: that legacy coefficient
lumps optical and collection losses into one number. The legacy definition is
preserved only for reproducibility, and is not silently reinterpreted.

The sensor integrates wavelength- and direction-dependent response before
generating charge. For photon irradiance per unit area, time and wavelength,
mean photoelectrons are exposure times pixel area times the spectral integral
of irradiance multiplied by QE. Direction weights already describe fractions
of sensor-plane photon irradiance. Generic CFA, pixel QE, fill-factor and old
550-nm angular multipliers are disabled in this replacement optical path.

The native RAW mosaic preserves R/Gr/Gb/B. The ISP uses a fixed central D65
reference, so changes in illumination or field angle remain visible instead of
being removed by per-frame white balance or image normalization.

## Microlens shape and routing interpretation

The default `geometry_source="reviewed_techinsights"` uses the locally reviewed
report overlay for IMX307/462/662. Detailed IMX662 DEP measurements supply its
1.3 µm lens height, 1.6 µm fitted curvature radius and identified green CFA
thickness of 0.50 µm. See [the evidence guide](sensor-techinsights-evidence.md)
for report pages, cross-report matching, conflicting local thicknesses and
unresolved AR optics. Other reports retain canonical geometry.

Microlens height, aperture, displacement and refractive index feed a paraxial
focusing diagnostic. Reported curvature where available (otherwise an
ellipsoidal curvature proxy), a Gaussian approximation to
the ray bundle, a diffraction width and absorption-depth-dependent displacement
estimate the fraction reaching the originating pixel's accepting aperture.
The accepting width is the square root of reported grid aperture area fraction
for IMX462/662, otherwise inferred from generated metal geometry, or supplied as
an explicit assumption. It is not a measured photodiode fill factor.

**Leaving the originating pixel does not establish that a photon is lost from
the sensor.** Under uniform illumination, neighboring pixels can collect that
light. The default `optical_assumptions={"routing_mode": "no_routing_loss"}`
therefore keeps net routing acceptance at one. Microlens own-site acceptance is
a diagnostic and does not attenuate nominal QE. In particular, the Gaussian
tails are not silently treated as camera shading or a measured QE reduction.
Changing lens height or shift can change this diagnostic while leaving nominal
QE unchanged; the model does not claim to know the resulting net angular loss.

An explicit `routing_mode="own_site_lower_bound"` multiplies response by the
own-site fraction to explore a pessimistic lost-neighbor scenario. Despite the
API name, this is **an engineering sensitivity scenario, not a guaranteed
physical or statistical lower bound**: the Gaussian model itself is unvalidated.

The `optical_assumptions` controls are:

| Setting | Meaning |
| --- | --- |
| `geometry_source` | `reviewed_techinsights` by default; `canonical_db` reproduces pre-overlay inputs |
| `cfa_thickness_scale` | Positive multiplier of DB filter thickness |
| `cfa_k_scale` | Positive absorption-index multiplier, scalar or red/green/blue mapping |
| `cfa_n_offset` | Refractive-index offset; resulting CFA index must remain above 1 |
| `lens_height_scale` | Height multiplier; scales a reported curvature radius inversely as a sensitivity assumption |
| `lens_shift_um` | Two lateral offsets in micrometres, each within a quarter pixel pitch |
| `acceptance_width_fraction` | Assumed accepting width divided by pitch, in (0, 1] |
| `diffraction_scale` | Positive multiplier of diagnostic Gaussian diffraction width |
| `microlens_enabled` | Enables the overlap diagnostic; false bypasses it with acceptance 1 |
| `routing_mode` | `no_routing_loss` by default; explicit `own_site_lower_bound` sensitivity |

`microlens_enabled=False` bypasses the overlap proxy; it does not physically remove
the lens polymer from the optical stack. Lens shape/shift controls alone do not
establish an absolute sensitivity change in the nominal path.
It must not establish product ranking or a center-to-edge color-shading claim.
Spatial and cross-color transfer are not fabricated to make this approximation
look complete.

Inspect the diagnostics independently of a scene:

```python
from pyisetcam.physical_estimate import (
    image_sensor_physical_model, physical_optical_response,
)

model = image_sensor_physical_model("DEF-2011-804")
budget = physical_optical_response(model, [450, 550, 650], theta_deg=35, phi_deg=0)
print(budget["eqe"])                      # nominal collected response
print(budget["own_site_acceptance"])      # unvalidated routing diagnostic
print(budget["own_site_eqe_lower_bound"]) # pessimistic scenario, not measured QE
```

The response also exposes `R`, `T`, layer absorptions, `energy_residual`,
`applied_routing_acceptance`, `routing_loss` and `own_site_routing_loss_bound`.
`microlens_acceptance` is a backward-compatible name for `own_site_acceptance`.
The planar energy budget closes before charge collection. Routing loss is never
relabelled as material absorption.

## Angular illumination and pupil assumptions

Provide either explicit rays or an exit-pupil geometry, not both:

```python
explicit_rays = {"directions": [
    {"theta_deg": 10, "phi_deg": 0, "weight": 0.5},
    {"theta_deg": 20, "phi_deg": 0, "weight": 0.5},
]}

exit_pupil = {
    "exit_pupil_distance_m": 0.020,
    "image_side_f_number": 4.0,
    "radial_samples": 3,
    "azimuth_samples": 8,
}
```

The first describes a common angular distribution for a local field patch.
The second varies direction across sensor position using the supplied exit-pupil
distance and diameter. It normalizes projected irradiance weights proportional
to `cos(theta) dOmega`. The `sensor_pupil_directions` helper uses a uniform
projected pupil in direction-cosine space; its image-side F-number is an explicit
angular-cone assumption. That local helper and a finite exit-pupil geometry are
different approximations, and neither infers pupil geometry from scene FOV.

Scene field of view is not chief-ray angle. Lens throughput and peripheral
illumination belong to the optical image; do not multiply a second cosine-fourth
law or an aperture loss into the normalized sensor direction weights. Supply
the actual image-side pupil information when it is known. The runtime supports
unpolarized 400-700 nm response up to 60 degrees without extrapolation; that
accepted domain is not a validation of the microlens approximation at 60 degrees.

## Readout profiles and overrides

The complete evidence and gain conventions are in
[readout-profile-sources.md](readout-profile-sources.md). Default low-gain
profiles use matched operating points from ASI462MC V1.1 and ASI662MC V1.0 camera
manuals. Their effective capacities are 11.2 ke- and 38.2 ke-, paired with total
camera RMS noise of approximately 2.45 e- and 6.9 e-. IMX307 remains an explicit
10 ke-/10 e- assumption with broad bounds. These observations do not constitute
bare-sensor measurements.

Use `operating_point="hcg"` to select the supported HCG point on the same camera
curve. Its lower capacity accompanies its lower noise. `assumptions` accepts
readout overrides such as `read_noise_e`, `full_well_e`, `dark_current_e_per_s`,
`gain`, `bit_depth`, `black_level_dn`, and fractional/electron FPN controls.
For example:

```python
config = image_sensor_db_config(
    "DEF-2206-805",
    model="physical_estimate_v1",
    operating_point="hcg",
    collection_efficiency=0.75,
    assumptions={"dark_current_e_per_s": 1.0, "prnu_fraction": 0.005},
    sensor_illumination=sensor_pupil_directions(20, 45, 4),
)
```

`gain=1` means no extra physical amplification beyond the selected operating
point. It is not the camera's gain register, nor conversion gain in V/e-.
Profile noise is converted from total camera noise to an approximate pre-ADC
Gaussian RMS by removing the source quantization variance; the simulator then
quantizes once. The 64-DN simulation pedestal changes the effective LSB slightly,
and provenance records that difference. An explicit `read_noise_e` override is
already pre-ADC and is not de-embedded again. Overrides update provenance and
do not retain an unsupported claim to match the source operating point.

Dark current, PRNU, fixed bias offsets and column FPN remain assumed quantities.
`prnu_fraction` is fractional RMS; `dsnu_e` means a fixed bias-offset RMS in
electrons per capture, not a temperature-dependent dark-current DSNU in e-/s.
The profile helper maps these to the final readout voltage scale and preserves
deterministic FPN identity across temporal frames.

## Running the comparison

From the repository root:

```bash
python tools/compare_physical_sensors.py --smoke --frames 4 --size 8 \
  --output reports/physical_sensor_e2e/smoke

python tools/compare_physical_sensors.py --frames 8 --size 32 \
  --output reports/physical_sensor_e2e/comparison
```

The standard run compares three sensors, D65/A/F11, uniform and Macbeth scenes,
F/2-F/8, center and oblique local field patches at 100 cd/m² and 4 ms. It uses
fixed exposure and fixed D65 calibration. Modes separate the previous DB estimate,
the fast model with controlled common readout, and the sensor's selected camera
readout proxy. This separates an optical-model change from a readout assumption.
The Python API `camerae2e_compare_physical_sensors` additionally accepts per-sensor
`readout_profiles` and `operating_points`, custom scene/angle grids and a DB `root`.

Reports contain `comparison.json`, `arrays.npz`, `index.html`, response/noise plots
and sensitivity results. Arrays include actual integer RAW samples and continuous
reference quantities. Standard A/F11 source spectra are integrated into visible
10-nm bins; F11's narrow peaks are not represented as exact high-resolution
spectroscopy. Collection/readout ranges and the explicit own-site routing
scenario are assumptions, not measured error bars. Dark and bright stress cases
check the behavior at noise and saturation limits.

Read the generated reasonableness status separately from measured readiness.
Energy conservation, monotonicity and photon/noise consistency are necessary
checks, but cannot establish sensor-product accuracy. Performance differences
smaller than the sensitivity range do not support a product ranking.

## Validation limits and FDTD status

The nominal path deliberately does not predict spatial PSF, optical/electrical
crosstalk, sensor MTF, microlens-derived net angular efficiency, or exact color
mixing between pixels. It excludes NIR, HDR, rolling-shutter motion, full TCAD,
blooming and calibrated temperature/readout-rate dependence. A separate IR-cut
model is necessary before making broadband visible-camera claims for a sensor
with substantial NIR response.

FDTD remains available for selected, numerically converged verification points.
`model="fdtd_estimate_v1"` requires an explicitly validated sensor-specific LUT
and illumination. Existing coarse 3D runs did not meet the numerical gates and
must not be promoted to performance data. Full three-sensor 3D validation remains
pending external compute resources; this fast model does not mark that work
complete. See [fdtd-cfa-e2e.md](fdtd-cfa-e2e.md) for the separate solver workflow,
and use the generated practical comparison report for the current measured-free
scenario results.
