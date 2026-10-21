# CFA and microlens spectral/angular sensor response

`fdtd_estimate_v1` carries wavelength-, angle-, azimuth- and native CFA-site-dependent
optical absorption into photoelectrons, integer RAW, and the existing ISP. It is
an explicit alternative to the [fast physical estimate](physical-sensor-e2e.md)
selected by the default `model="auto"`. A generated campaign
manifest, a low-resolution process check, or a synthetic unit fixture is not a
validated sensor LUT. The current calculation status is recorded in
[the implementation report](../reports/fdtd_sensor_e2e/implementation.md).

## Physical model and interpretation

The canonical source is the catalog optical-QE database's RGB complex index
`n(λ) + i k(λ)`. The older FDTD CSV uses different optical constants and is not
mixed into this model. Each monochromatic Meep calculation evaluates those
constants at its own wavelength. The material bundle also contains silicon,
microlens and passivation indices, provenance, uncertainty assumptions and hashes.
Beer–Lambert transmission checks the absorption-only data; an independent
complex-index planar solver checks reflection, transmission and absorption by layer.
A color filter's absorbed power is a loss, not photoelectrons.
All three selected sensors currently share the same proxy RGB `n,k` curves.
Differences arise from thickness and stack geometry; the DB does not establish
product-specific measured CFA dispersion. The ranges below are sensitivity
bounds, not statistical confidence intervals.

| Sensor | CFA thickness | Thickness range | Total optical stack |
| --- | ---: | ---: | ---: |
| IMX462 / DEF-2011-804 | 0.500 µm | 0.450–0.550 µm | 2.500 µm |
| IMX307 / DEF-1807-801 | 0.650 µm | 0.520–0.780 µm | 2.400 µm |
| IMX662 / DEF-2206-805 | 0.475 µm | 0.420–0.530 µm | 2.600 µm |

IMX307's canonical CFA thickness differs from its older geometry estimate. Its
passivation remainder is therefore 0.934 µm to preserve total optical height;
this is an inferred layer allocation. Baseline microlenses are centered upper-half
ellipsoids, with zero lateral shift and DB-derived height and lateral extent.
Height, shift and dispersion enter the 3D model and sensitivity variants, but
these are DB-based estimates, not measured production surface profiles.

A periodic 2×2 Bayer cell preserves R, Gr, Gb and B separately. The optical output
is absorbed power in the full silicon below each native pixel, divided by
incident power on the whole cell in a separate empty reference calculation.
For a uniform plane wave, the effective per-pixel photon response is
`4 × absorption_cell_fraction × collection_efficiency`. A site's value before
collection can exceed one through redistribution from a neighboring cell site;
it is not clipped. Cell-level energy conservation and passivity constrain total
power. Optical redistribution under uniform illumination does not establish a
spatial crosstalk kernel or sensor MTF.
Applying this local response to an image assumes irradiance varies slowly over
the repeated pixel cell.
Silicon is a homogeneous absorption volume. DTI, metal grids, detailed AR
multilayers and internal photodiode geometry are not resolved; their optical
effects can matter even when a spatial crosstalk kernel is outside this task.
The lumped passivation layer and scalar collection efficiency do not establish
that these omitted structures are negligible.
The finite silicon slab is currently air-backed. This is an explicit boundary
approximation, not the actual front-side dielectric/metal stack or a
semi-infinite substrate; its reflection/interference can affect spectral and
angular response and requires separate boundary-model sensitivity before
product-level interpretation.

Collection efficiency defaults to 0.75, with 0.5–1.0 sensitivity bounds. It is
separate from optical absorption. The model does not establish charge-transport
collection, product QE, read noise, full well, dark current, HDR, rolling shutter,
NIR response or measured product accuracy. Those electrical/readout values retain
explicit assumptions from the [digital RAW API](sensor-capture-api.md).

## Runtime contract

```python
from pyisetcam import image_sensor_db_config, camerae2e_run_scenario

config = image_sensor_db_config(
    "DEF-2011-804",
    model="fdtd_estimate_v1",
    lut_path="reports/validated/imx462.json",
    sensor_illumination={"directions": [
        {"theta_deg": 20, "phi_deg": 45, "weight": 1,
         "polarization": "unpolarized"},
    ]},
    collection_efficiency=0.75,
    assumptions={"read_noise_e": 10, "full_well_e": 10000},
)
config["scenario"]["sensor"] = {"integration_time": 0.004, "noise_flag": 2}
result = camerae2e_run_scenario(config["scenario"], scene=scene, seed=0)
```

The loader requires sensor identity, current material and geometry hashes,
complete rectangular oblique sampling with a collapsed phi=0 normal anchor,
both s/p polarizations, finite passive power,
and numerical validation evidence. Wavelength and polar-angle extrapolation are
errors; azimuth is periodic. Version 1 combines s/p equally for unpolarized input.
Explicitly polarized illumination is rejected rather than silently averaged.

Incident directions may be supplied as normalized weighted rays. The low-level
API also supports explicit exit-pupil distance and image-side F-number. Camera
field of view is not substituted for chief-ray angle. Angular weights describe
the distribution of already-delivered irradiance and do not apply another
aperture/vignetting throughput factor. Runtime interpolation occurs before
wavelength integration and charge/readout. Generic CFA/QE/IR-cut/fill/OCL factors
and the old radial FDTD and TCAD factors cannot be multiplied on top. Changes to
CFA, pitch, photodiode area or reference QE after applying a LUT are rejected.

R/Gr/Gb/B remain distinct through native capture. Four-channel color conversion
uses fixed central D65 calibration; angle, illuminant and frame changes do not
refit white balance or normalize image maxima. Sensor-space AWB statistics
combine the two green sites explicitly when that separate ISP feature is used.

## Numerical acceptance

- Energy balance: absolute error ≤ 0.01 of incident cell power.
- Spatial mesh, simulation duration and PML checks: response and normalized
  angular response changes ≤ 3%, including minimum wavelength and maximum angle.
  Weak-response handling must be explicit in evidence, not hidden by a bright channel.
- Interpolation: independent off-grid FDTD checks, with numerical comparison
  records and sample counts. Merely interpolating the LUT's own nodes is insufficient.
- Pupil integration in the comparison tool: 3×8 versus 6×16 rays, escalating to
  12×32 and 24×64 if necessary; response change ≤ 1% with a 0.01 response floor.
- Missing checks keep the LUT `unverified`; measured accuracy remains a separate
  false flag even after numerical checks pass.

Initial sampling is 7 wavelengths × 33 directions × 2 polarizations = 462
production jobs per sensor. Final sampling is 31 wavelengths × 65 directions × 2
= 4,030 jobs per sensor, plus independent calibration/convergence jobs. Each job
includes its empty reference. Additional refinement is required when direct
holdouts fail; computation cost alone cannot relax an acceptance threshold.

## Reproduce materials and plan a campaign

Use the main CameraE2E Python environment for these commands. The Meep worker is
standalone and can run in its separate Python 3.10 environment.
Install the controller's process-monitor dependency with `pip install -e '.[fdtd]'`.
The worker environment must separately provide Meep and NumPy; Meep is not
installed or changed by the campaign tool.

```sh
PYTHONPATH=src python tools/verify_cfa_materials.py \
  --output-dir reports/fdtd_sensor_e2e/materials

PYTHONPATH=src python tools/run_sensor_fdtd_campaign.py \
  --output-dir reports/fdtd_new_campaign --sampling final --resolution 80

PYTHONPATH=src python tools/run_sensor_fdtd_campaign.py \
  --output-dir reports/fdtd_new_campaign --sampling final --resolution 80 \
  --run-pilot planar_calibration --max-wall-seconds 120
```

Campaigns freeze code and material bundles, hash their jobs, and refuse to reuse
results from a different job. Reusing an identical campaign resumes it. If code,
materials or settings change, use a new directory. The local watchdog permits
one solver job, at most four CPU threads, 24 GiB RSS and two hours. Estimated
oversized jobs remain pending for an external high-memory machine; local limits
are not increased automatically. See the generated campaign's resource report
and runner `--help` for execution, status, assembly and external transfer.

## End-to-end comparison

```sh
PYTHONPATH=src python tools/compare_fdtd_sensors.py \
  --lut DEF-2011-804=reports/validated/imx462.json \
  --lut DEF-1807-801=reports/validated/imx307.json \
  --lut DEF-2206-805=reports/validated/imx662.json \
  --output reports/fdtd_comparison
```

Defaults compare DB response, normal-incidence FDTD response and pupil-integrated
FDTD response under D65/A/F11; uniform and Macbeth scenes; F/2, F/4, F/8; CRA
0°, 10°, 20° along x and diagonal directions; 128×128 matched physical output;
32 temporal frames; 100 cd/m² and 4 ms. AE is off. All three sensors would produce
810 cases and 25,920 temporal samples. `--smoke --size 16 --frames 2` is a smaller
connection check. Missing or rejected LUTs produce a pending report and CLI exit
code 2, with no substituted synthetic measurements.

JSON includes photoelectrons, temporal SNR, saturation, R/G and B/G, center-to-edge
signal and color change, pupil convergence and source/readout provenance. NPZ
contains actual int32 RAW samples and float32 means/variances. PNG/HTML figures
use a shared display scale and fixed D65 calibration. Macbeth uses the same
reflectance set as color calibration, so its residual is not an independent
color-accuracy measurement; A/F11 change the illuminant but not the test surfaces.
R/Gr/Gb/B native-site means and temporal RAW variances are retained separately.
Burst-averaged well/ADC clipping above 0.1% makes SNR ineligible for comparison
curves; the underlying diagnostic remains in JSON, with a clipping label.

The A and FL11 spectra are packaged from CIE datasets, with source SHA-256 and
MD5 verified against CIE metadata. FL11's 5 nm spectrum and A's 1 nm spectrum
are integrated into runtime wavelength bins, avoiding point-sampling of narrow
peaks. The current visible model still has a 10 nm band approximation and does
not establish monochromatic/narrow-line color accuracy.

- [CIE standard illuminant A dataset](https://cie.co.at/datatable/cie-standard-illuminant-1-nm)
- [CIE fluorescent illuminant dataset](https://cie.co.at/datatable/relative-spectral-power-distributions-illuminants-representing-typical-fluorescent-lamps)

The selected data columns in `cie_illuminants_visible.json` retain CIE attribution
and the **CC BY-SA 4.0** license. The JSON records the column selection and visible
range cropping; bin averaging is performed at runtime.

## External execution, resumption and sensitivity

Export a frozen campaign with the current controller. The export preserves
original job identity while resolving material and code paths relative to the
copied package; the remote machine does not need this user's `/Users` paths.
It includes `jobs.jsonl`, per-job memory estimates, `run_job.sh` and import
instructions. Different campaigns cannot share an existing export directory.

```sh
python simulations/fdtd_tcad/run_sensor_response.py prepare-external \
  --manifest reports/fdtd_sensor_e2e/solver_v3/campaign.json \
  --output-dir reports/fdtd_external

# After running selected jobs under the external server's scheduler and
# copying returned JSON files to reports/returned_fdtd_results:
python simulations/fdtd_tcad/run_sensor_response.py import-results \
  --manifest reports/fdtd_sensor_e2e/solver_v3/campaign.json \
  --results-dir reports/returned_fdtd_results

python simulations/fdtd_tcad/run_sensor_response.py merge \
  --manifest reports/fdtd_sensor_e2e/solver_v3/campaign.json \
  --report-code DEF-2011-804 --output reports/validated/imx462.json
```

`merge` may write an **unverified** partial response. Its exit success means the
assembly operation finished, not that all gates passed. The runtime loader
still rejects it. `status` reports completed/pending roles. Assembly's refinement
recommendation supplies new grid steps after failed direct holdouts; `plan`
accepts `--wavelength-step-nm`, `--theta-step-deg`, `--phi-step-deg` and
`--sampling refined`. Minimum supported steps are 5 nm, 2.5°, 11.25°; failure at
those steps remains a failure, requiring numerical/model investigation.

```sh
python tools/prepare_sensor_fdtd_sensitivity.py prepare \
  --output-dir reports/fdtd_sensitivity_new

python simulations/fdtd_tcad/run_sensor_response.py prepare-external \
  --manifest reports/fdtd_sensitivity_new/sensitivity_campaign.json \
  --output-dir reports/fdtd_sensitivity_external

python tools/prepare_sensor_fdtd_sensitivity.py assess \
  --manifest reports/fdtd_sensitivity_new/sensitivity_campaign.json \
  --output reports/fdtd_sensitivity_new/sensitivity_screen.json
```

For each sensor, 24 one-at-a-time variants plus its baseline form 25 immutable
bundles: each RGB thickness low/high; each RGB `k × 0.8/1.2`; each RGB
`n ± 0.05`; lens height `× 0.9/1.1`; lens x/y shift `±0.05 pitch`.
There are 30 anchors per bundle: 450/550/650 nm; normal and 20°/35° along x/y;
s/p. Across three sensors this is 75 bundles and 2,250 jobs. Thickness changes
preserve the common CFA top/lens plane by a complementary per-channel change
in passivation thickness; no air gap is introduced. Lens shift wraps the
periodic optical geometry while Si collection regions stay fixed.

The sensitivity assessor waits for matched baseline/variant anchors, checks
finite passive energy accounting, then reports native response and angular-ratio
changes. Changes above 5% require expanded sweeps. It also computes
`4 × optical cell fraction × eta` bounds at eta 0.5, 0.75 and 1.0 from completed
anchors. These are electrons per incident pixel-equivalent photon, not captured
electrons without an exposure/irradiance input. No numerical bounds are produced
for missing runs. Anchor sensitivity alone never promotes an E2E LUT to validated.
