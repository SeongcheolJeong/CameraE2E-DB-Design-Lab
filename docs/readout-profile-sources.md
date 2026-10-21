# Practical sensor readout profiles

The practical optical model uses discrete readout operating points. IMX462 and
IMX662 defaults borrow paired charge-capacity and noise observations from
specific complete ZWO cameras. They are **camera proxies, not measured Sony
bare-sensor specifications**. IMX307 remains an explicitly unmeasured assumption.
An optical simulation does not validate these electronic parameters.

| Sensor / source profile | Point | Vendor register | Effective charge capacity | Published total noise | Simulated pre-ADC noise |
| --- | --- | ---: | ---: | ---: | ---: |
| IMX462 / ASI462MC V1.1 | `low_gain` (default) | 0, LCG | 11,200 e- | about 2.45 e- RMS | 2.320 e- RMS |
| IMX462 / ASI462MC V1.1 | `hcg` | 100, HCG | about 3,500 e- | about 1.00 e- RMS | 0.969 e- RMS |
| IMX662 / ASI662MC V1.0 | `low_gain` (default) | 0, LCG | 38,200 e- | about 6.90 e- RMS | 6.356 e- RMS |
| IMX662 / ASI662MC V1.0 | `hcg` | 300, HCG | about 1,229 e- | about 1.00 e- RMS | 0.996 e- RMS |
| IMX307 / `generic_assumption` | `assumed_reference` | unknown | assumed 10,000 e- | unavailable | assumed 10 e- RMS |

The 662 low-gain profile therefore has a larger usable charge range **and higher
read noise** than the 462 low-gain profile. Combining 38.2 ke- with the advertised
minimum 0.8 e- would describe neither selected operating point.

## Evidence and extraction

The package contains factual numeric extractions and SHA-256 digests; source PDFs
are not redistributed. Access date is 2026-09-05. Runtime is offline and does not
silently replace a versioned profile when a vendor webpage changes.

- [ZWO ASI462 Manual EN V1.1](https://i.zwoastro.com/zwo-website/manuals/ASI462_Manual_EN_V1.1.pdf),
  pages 5-7. Page 5 gives 11.2 ke- and a 12-bit ADC. Page 6 defines read noise as
  including ADC quantization. The page 7 graph pairs fullwell, electrons/ADU and
  noise at the same gain register; HCG starts at 80. The chosen HCG point is 100,
  beyond the transition. Gain-zero noise is read as approximately 2.45 e- from
  the chart, while the specification table gives a broader maximum of 2.65 e-.
  SHA-256: `acdfbf644d3b2e8de315c4090f2fb5275b2698a111ec390ef66f4e31fe02302e`.
- [ZWO ASI662 Manual EN V1.0](https://i.zwoastro.com/zwo-website/manuals/ASI662_Manual_EN_V1.0.pdf),
  pages 5-7 and 10. Gain-zero values are paired with the page 7 graph. Its
  fullwell axis lacks numeric labels; at gain 300, the explicit electrons/ADU
  axis gives approximately 0.30 e-/ADU. The 1,229 e- effective saturation estimate
  is `0.30 * 4096`; it is not a separately measured photodiode storage capacity.
  The selected HCG noise is approximately 1.0 e-, not the minimum at higher gain.
  SHA-256: `517bbfde0c620e73a19f9c02cb7116ca972e4b3201245581c7adf6b2393d5ddc`.
- [Sony IMX307LQD/LQR Flyer02](https://www.sony-semicon.com/files/62/pdf/p-12_IMX307LQD_LQR_Flyer02.pdf),
  page 2. It supports 10/12-bit operation but reports saturation and sensitivity
  in digital units. Without the corresponding e-/DN conversion these numbers
  cannot establish electron capacity or read noise. No electron-domain product
  values were inferred from them.
  SHA-256: `36c1adc67042400ac84e5d65daf00e7bbbe9468c1cf4517869e4b1dcbaf5bff5`.

The ASI662 V1.0 MC-only manual puts the HCG transition at register 252. The later
[combined MC/MM product page](https://us.zwoastro.com/products/asi662mc) states 200.
The selected profile pins V1.0; these revisions are not mixed. Manufacturer chart
temperature, readout rate and exposure are unspecified. The 12-bit mode is
selected without claiming a particular frame rate. RAW16 transport does not
increase the ADC precision.

Curve values were manually read from the plotted markers. The JSON carries a
range for every model field. Digitization ranges and broader assumed engineering
ranges are scenario bounds, **not confidence intervals or measured production
distributions**. Only two explicit operating points per camera are supported;
the code does not invent continuous gain curves or interpolate across HCG.

## ADC and gain conventions

The simulator adds Gaussian electron-domain read noise before quantization.
Feeding total camera read noise directly would add ADC quantization twice.
For these source profiles it instead uses the approximation

`pre_ADC_RN = sqrt(reported_total_RN**2 - source_electrons_per_ADU**2 / 12)`.

This assumes independent white quantization noise. It is an approximate
decomposition, not a measured separation of analog noise. The original total
noise, source LSB, derivation and uncertainty bounds remain in provenance.
The simulation reserves a 64-DN black pedestal, so its electron step is
`full_well_e / ((2**bits - 1 - black_level_dn) * gain)` and differs slightly from
the nominal source step. The predicted resulting total is recorded separately.
At sub-LSB signal or near clipping the white-noise formula is not exact; actual
RAW output is explicitly quantized. Tests sample actual dark RAW to verify the
default total RMS is reconstructed within 3%, without double counting.

`gain=1` means no extra physical amplification beyond the selected operating
point. Vendor register 100 is not a multiplier of 100 and is not fed to that
field. `reported_electrons_per_adu` is camera system gain, while pixel conversion
gain in V/e- is unknown. The existing simulation voltage scale remains synthetic
(`voltage_swing/full_well_e`). `full_well_e` represents effective usable charge at
the selected camera operating point, including possible ADC limitation. It does
not assert that physical charge storage shrinks when an external amplifier gain
changes. Blooming is outside this simple model.

## Remaining assumptions

All profiles explicitly declare the following nominal values and ranges:

| Quantity | Nominal | Scenario range | Meaning |
| --- | ---: | ---: | --- |
| Dark current | 10 e-/s | 0-100 e-/s | Unknown; no calibrated temperature law |
| Residual PRNU RMS | 0.5% | 0-2% | Assumed fractional spatial response error |
| Fixed bias-offset RMS (`dsnu_e`) | 1 e- | 0-5 e- | Per-capture offset, not dark-current DSNU in e-/s |
| Column response RMS | 0 | 0-0.5% | Unknown; nominally disabled |
| Column fixed offset RMS | 0 e- | 0-2 e- | Unknown; nominally disabled |
| Digital black pedestal | 64 DN | fixed | Simulation policy, not a vendor offset register |

For IMX307 the unmeasured capacity range is 5-40 ke- and the pre-ADC noise range
is 1-15 e-. These broad assumptions require sensitivity reporting; they do not
justify ranking IMX307 against the camera proxies as if all three were measured.

## API

```python
from pyisetcam.sensor_readout_profiles import (
    apply_readout_profile, resolve_sensor_readout,
)

resolved = resolve_sensor_readout("IMX462", operating_point="hcg")
sensor = apply_readout_profile(sensor, resolved)

# Explicit scenario assumptions replace source equivalence in provenance.
resolved = resolve_sensor_readout("IMX662", overrides={"dark_current_e_per_s": 1.0})
```

The result contains `readout`, `fixed_pattern`, canonical sensor identity, profile
and operating-point names, and per-field provenance under `provenance.fields`.
An optional `root` selects a custom sensor catalog for additional identities;
those without a matching camera profile receive `generic_assumption` explicitly.
The six `readout` keys match `configure_e2e_sensor`. The four fixed-pattern
controls use fractional gain RMS or electron RMS and are converted to the
existing voltage fields after the final effective capacity is known.
`apply_readout_profile` keeps cached mean photocharge and deterministic FPN seeds.
It invalidates FPN maps when the operating point or scale changes and records
effective overrides. Temporal output must then be resampled normally.

The generic assumption can be selected explicitly for controlled optical
comparisons. Passing a camera profile belonging to another sensor is rejected.
The existing `db_estimate_v1` defaults are not silently changed by this module.
