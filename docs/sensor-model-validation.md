**Sensor model contracts and validation — September 2026**

The sensor supports deterministic research experiments. Correcting its numerical
contracts does not supply measured sensor calibration, a converged optical LUT,
or a calibrated PPD/TG/FD/readout model. Existing generated reports may contain
the earlier normalization, noise, and dynamic-range results.

The current CameraE2E defaults and the DB optical estimate are documented in
[DB sensor E2E](db-sensor-e2e.md). E2E scenarios and HW ISP frames now use an
explicit digital RAW contract; unmarked low-level sensor calls retain their
legacy contract. This document also records the earlier FDTD/TCAD audit fixes.
See [capture API and migration examples](sensor-capture-api.md) for output units,
readout settings and explicit legacy selection.

**Optical response**

`fdtd_sensor_lut_response(..., normalize_to_center=True)` now returns
`R(wavelength, case) / R(wavelength, center)`. Historical `*_to_first` columns
describe a complete sweep's first row and are not used as center normalization.
A missing, zero, negative, or nonfinite center cannot define a normalized ratio
and raises an error.

The FDTD adapter multiplies the existing sensor spectral QE. It does not replace
it with measured external QE. In `qe+field` mode, the QE stage uses the central
spectral reference and the field stage applies only relative shading. With the
default normalization, the central QE multiplier is unity at each wavelength.
Setting `normalize_to_center=False` explicitly applies raw central optical
absorption as a proxy multiplier once; it is not an assertion that the resulting
combined QE is an absolute calibrated sensor response.

Field and crosstalk act on spectrally integrated sensor signals. They use the
available wavelength nearest 550 nm by default, or the nearest wavelength to
`wavelength_nm` supplied to `sensor_attach_fdtd_lut`. This remains a separable
spectral/spatial approximation. Radial field samples at equal radii are averaged
deterministically across directions. Distinct optical shift variants at the same
field/angle must be separated before constructing a radial map. Reordering LUT
rows must not change the output. A missing crosstalk kernel still represents an
identity response, with a validation warning.

Numerical convergence requires a convergence report. A resolution value alone
does not prove convergence; missing evidence produces a warning even when the
pixel grid is fine. Full resolution/runtime/PML convergence and measured optical
stack/material inputs remain necessary for quantitative use.

**Electrical collection**

For a 2D imported generation map, the adapter computes

```text
eta(case, wavelength) = I_photo[A/cm] / (q[C] * integral G[cm^-3 s^-1] dx[cm] ddepth[cm])
relative_scale = eta(case, wavelength) / eta(center, wavelength)
```

The integration uses the exported cell-center coordinates and the collection
summary's `generation_map_scale`. This removes the optical generation amplitude
already accounted for upstream. `normalize_to_center=False` returns dimensionless
eta, rather than treating the number of A/cm as a signal multiplier.

`case=None` selects an actual center case. Multiple spectral entries require an
explicit `wavelength_nm`, and normalized comparisons use the same wavelength.
Nonfinite values, invalid coordinates, mismatched generation maps, missing
required files, ambiguous cases, and nonpositive reference current/generation
are rejected independently of `allow_proxy_accuracy`.

`allow_proxy_accuracy=True` permits a numerically valid, uncalibrated framework.
It does not permit malformed input. Efficiencies above one produce a warning
without being silently clipped into an apparently physical result. Strict
accuracy requires that the accuracy gate cover the attached collection summaries
and their generation map. A custom G/collection bundle does not automatically
inherit the default bundle's accuracy gate.

**Fixed patterns, frames, and output codes**

Pixel and column FPN maps persist for a simulated sensor and are honored by the
main and staged noise paths. Temporal shot/read randomness uses a separate seed.
The fixed-pattern seed defaults to zero for reproducible device realizations;
change it explicitly to model a different realization:

```python
sensor = sensor_set(sensor, "fixed pattern seed", 17)
first = sensor_compute(sensor, oi, seed=101)
second = sensor_compute(first, oi, seed=102)
```

Changing the fixed-pattern seed clears stored FPN maps. Changing a temporal seed
does not. Explicit calibration maps are used until their corresponding model
settings invalidate them. Repeated samples retain the same spatial maps; with
only fixed pattern noise, temporal variance is zero. The existing gain-FPN
voltage model remains an approximation of spatial gain variation, not a full
microscopic model of QE variation and readout circuitry.

`camera_compute(..., seed=...)` forwards the temporal seed to the sensor.
`camerae2e_run_scenario(..., seed=...)` controls the experiment, taking precedence
over an embedded HW ISP seed. HW ISP frames use `seed + frame_id`, so repeating
the experiment is deterministic while successive frames have independent
temporal draws. FDTD/TCAD `enabled=False` disables an inherited attachment and
does not read the supplied artifact path.

Linear N-bit ADC outputs are bounded to `0 .. 2**N - 1` and use the same
round-to-nearest endpoint mapping in direct and staged paths. Recomputing an
analog result clears stale digital output. `sensor_dr` uses `20*log10` of the
voltage-to-noise-standard-deviation ratio; dark shot variance in V² includes the
conversion gain in V/electron. For a 1 V swing and 1 mV read noise with no dark
signal, both sensor and pixel DR are 60 dB.

**Sensor DB selection**

`image_sensor_db_config(sensor_id)` defaults to `model="auto"`: canonical RGB
Bayer records use `physical_estimate_v1`, with reviewed local TechInsights
geometry where available, and other supported records use `db_estimate_v1`.
The physical path uses Fresnel/Snell/absorption transport and an explicit
charge-collection assumption, with FDTD/TCAD disabled. Unknown modalities,
CFA layouts or required geometry produce an explicit exclusion. The resulting
EQE replaces generic optical factors once; it is not measured product QE.

The previous artifact attachment workflow requires `model="legacy"`. It attaches
only declared sensor-specific artifacts unless the shared reference proxy is
explicitly requested. When no binding exists, the hybrid configuration disables
both physics layers; an inherited Camera's unrelated physics is not retained:

```python
reference = image_sensor_db_config(sensor_id, model="legacy", allow_shared_proxy=True)
specific = image_sensor_db_config(sensor_id, model="legacy", require_sensor_specific=True)
```

The latter rejects absent or unrelated bindings; it does not certify measured
accuracy. `strategy="lut_only"` rejects a configuration with no available LUT.
The configuration, FACA report, and optimization source retain `physics_binding`
with scope, attachment, and sensor-association information. Overrides are
reconciled against the declared artifacts so an unrelated replacement cannot
retain a sensor-specific claim.

A catalog record can associate artifacts with its exact sensor ID:

```json
{
  "physics_artifacts": {
    "sensor_id": "the_record_sensor_id",
    "lut_path": "runs/this_sensor/camera_lut.json",
    "generation_map_path": "runs/this_sensor/tcad_generation_map_2d.npz",
    "collection_summary_paths": ["runs/this_sensor/center/summary.json"],
    "accuracy_gate_path": "runs/this_sensor/tcad_accuracy_gate.json"
  }
}
```

Paths resolve against the catalog directory. Omit unavailable components rather
than providing paths to another sensor. Declared missing files or a mismatched
sensor ID raise errors, without falling back to reference files. Source
association, numerical validation, and measured calibration are separate facts.

**Compatibility and checks**

Physical invariants have dedicated regression coverage in
`tests/unit/test_sensor_physical_invariants.py`, `test_fdtd_sensor.py`,
`test_tcad_sensor.py`, `test_system_faca.py`, and `test_image_sensor_db.py`.
The broader pipeline, HW ISP, optimizer, dataset, and stored sensor-reference
cases also exercise these changes.

Some changes intentionally differ from pinned MATLAB behavior: DR's former
10-log voltage ratio, ADC's out-of-range endpoint, and resampling DSNU every
frame. Stored `.mat` references are preserved. Any reference comparison that
uses a corrected DR definition or the historical independent-pattern ensemble
must state that comparison contract; it does not establish unchanged physical
behavior for repeated frames from one sensor.

The [EMVA 1288 Linear definitions](https://www.emva.org/wp-content/uploads/EMVA1288Linear_4.0Release.pdf)
provide a reference for QE, gain, temporal noise, and spatial nonuniformity.
[Meep's official guidance](https://meep.readthedocs.io/en/latest/FAQ/)
describes numerical convergence and optical normalization. Applying those
definitions to measured data remains separate from software regression tests.
