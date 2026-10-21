# Measured MTF to PSF and back

`measured_mtf_to_psf()` constructs a finite, nonnegative, unit-sum PSF from a
normalized radial MTF curve, then evaluates the returned PSF's MTF at every
original frequency along 0, 45, 90 and 135 degrees. A returned candidate is
accepted numerically only when both the maximum absolute residual and RMSE meet
the requested tolerances. Failure retains the candidate and its diagnostics.

**This is a radial, zero-phase approximation, not a unique reconstruction of a
lens PSF.** MTF alone omits phase. Sparse or incomplete frequency measurements,
the chosen high-frequency continuation, finite support and square-grid sampling
can materially change the result. Input consistency does not establish physical
camera accuracy or sampling convergence.

## CSV workflow

Supply a UTF-8 CSV with columns `frequency,mtf` (additional columns are retained
in the original copy but not interpreted). Frequencies must start at zero,
increase strictly and not exceed the chosen sample pitch's axial Nyquist
frequency. The normalized MTF must begin at 1 and stay in `[0, 1]`. Missing DC,
duplicates, NaN, infinity and invalid values are rejected without silently
normalizing, sorting or clipping the input. MTF50 alone is insufficient.

```csv
frequency,mtf
0,1
20,0.95
40,0.81
60,0.62
80,0.43
100,0.27
```

```bash
PYTHONPATH=src python tools/convert_measured_mtf.py measurement.csv \
  --frequency-unit cycles/mm --sample-pitch-um 2 \
  --kernel-size 129 --high-frequency zero --negative-policy project \
  --max-abs-error 0.02 --rmse-tolerance 0.01 \
  --measurement-domain optical --source-metadata measurement_conditions.json \
  --output /tmp/measured-mtf-run-001
```

`--source-metadata` is optional. It accepts a JSON object for camera/lens identity,
aperture, focus, wavelength, field position, measurement method, uncertainty and
other acquisition conditions. The CLI records these as user-supplied provenance;
it does not independently verify the measurement. `--measurement-domain` is
`optical`, `camera_raw`, `camera_isp` or the default `unspecified`.

The example curve above illustrates the format; its abrupt nonzero endpoint may
fail the default limits. A failed comparison is retained for inspection and is
not silently made successful by relaxing the tolerances.

Every run uses a **new output directory**; existing paths are refused. Outputs
are calculated before the directory is created:

| File | Contents |
| --- | --- |
| `input.csv` | Exact source bytes, with original path, size and SHA-256 in the report |
| `psf.npy` | Centered, nonnegative, unit-sum discrete impulse weights |
| `report.json` | Complete result, including PSF, original samples, per-direction returned MTF and residuals, limits, assumptions, provenance and diagnostics |
| `roundtrip.csv` | One row per original sample and direction; signed residual is returned minus input |
| `comparison.png` | Log-scale PSF, input versus returned MTF and residual plots |
| `source_metadata.json` | Exact optional metadata bytes, with SHA-256 in the report |

The report also records SHA-256 hashes of the CLI and the loaded conversion
module so the export identifies the algorithm implementation used for that run.

Exit status is **0** for numerical consistency passing both limits, **2** for a
saved candidate exceeding either limit, and **1** for input/conversion/export
errors. Argument syntax errors also use argparse's exit status 2 but produce no
candidate. The PNG's PSF display floor only affects visualization; the NPY and
JSON preserve the computed values.

For `cycles/pixel`, use `--measurement-pixel-pitch-um` to specify the camera pixel
pitch used by the measurement. It defaults explicitly to `--sample-pitch-um`
when omitted. Keep the measurement pixel pitch fixed when refining the PSF
sample pitch, or convert the curve to `cycles/mm` first. The measurement pitch
option is rejected for `cycles/mm`. The PSF sample pitch and kernel side
determine spatial support and frequency-grid spacing. Repeat at larger support
and, where appropriate, finer pitch to assess convergence. Lower PSF sample
pitch extends Nyquist without changing the measurement's frequency convention
when measurement pixel pitch is supplied.

## Python API

```python
import numpy as np
from pyisetcam import measured_mtf_to_psf, psf_to_mtf, otf_to_psf

frequency = np.linspace(0, 250, 501)  # cycles/mm
mtf = np.exp(-2 * np.pi**2 * (0.003 * frequency)**2)
result = measured_mtf_to_psf(
    frequency, mtf,
    sample_pitch_um=2.0,
    kernel_size=129,
    frequency_unit="cycles/mm",
    high_frequency="zero",
    negative_policy="project",
    max_abs_error=0.02,
    rmse_tolerance=0.01,
    source={"kind": "analytic_test_fixture", "sigma_um": 3.0},
)
assert result["roundtrip"]["passed"]
psf = result["psf"]

# Independent forward evaluation, using the exact discrete Fourier sum at
# requested frequencies; no FFT-grid interpolation or radial averaging.
forward = psf_to_mtf(
    psf, sample_pitch_um=2.0, frequency=frequency,
    frequency_unit="cycles/mm", angles_deg=[0, 45, 90, 135],
)

# For a supplied complex OTF, preserve phase and explicitly choose the
# treatment of nonphysical inverse intensity. The input must be unshifted
# FFT order; psf_to_mtf returns a centered OTF, hence ifftshift here.
inverse = otf_to_psf(np.fft.ifftshift(forward["otf"]), negative_policy="raise")
np.testing.assert_allclose(inverse["psf"], psf, atol=1e-12)
```

`measured_mtf_to_psf` returns `psf`, `sample_pitch_um`, `spatial_axis_um`, `input`,
`roundtrip`, `diagnostics`, `assumptions`, `warnings`, and
`physical_validation="not_established"`, plus schema/method identifiers.
`roundtrip` contains original frequencies, the four angles, `mtf_by_angle` and
`residual_by_angle` arrays shaped `(4, sample_count)`, observed maximum error and
RMSE across all samples/directions, requested limits, and `passed`.

Both conversion APIs accept optional `measurement_pixel_pitch_um` for
`cycles/pixel`; keep it fixed across grid-refinement runs. `psf_to_mtf` returns
centered `otf`, `mtf_2d`, `frequency_x`, `frequency_y`, `frequency_unit` and the
effective `measurement_pixel_pitch_um`. With `frequency` supplied it also returns `frequency`,
`angles_deg`, and `mtf_by_angle`. It normalizes positive kernel mass; input must
already be real, finite and nonnegative. `otf_to_psf` returns `psf` and
`diagnostics` and normalizes positive real DC.

The forward transform uses the centered Fourier convention and normalized
complex OTF magnitude described in the official
[Prysm OTF API reference](https://prysm.readthedocs.io/en/latest/api/otf.html).
Prysm is a reference here, not an additional runtime dependency.

For an accepted **optical** curve with a known measurement wavelength, the PSF
can be attached to the existing shift-invariant optics interface. Supply the
actual wavelength; 550 nm below is an example for a measurement known to have
been made at 550 nm, not an inferred wavelength for an arbitrary input curve.

```python
from pyisetcam import oi_set

measured_wavelength_nm = 550.0  # Replace with the known acquisition wavelength.
if not result["roundtrip"]["passed"]:
    raise ValueError("MTF round-trip failed; do not accept this candidate.")
oi = oi_set(oi, "psf data", {
    "psf": result["psf"],
    "wave": [measured_wavelength_nm],
    "sample_spacing_um": result["sample_pitch_um"],
})
```

Keep the result/report beside the optics configuration as calibration evidence.
A RAW or ISP system SFR is not thereby isolated as optical blur; assigning it to
the lens and applying sensor/ISP effects again can double-count those effects.
Broadband measurements also do not establish a monochromatic wavelength model.

## Why the inverse no longer uses absolute intensity

The old `abs(ifft2(otf))` reflected negative intensity into positive intensity
and could silently alter the MTF. The new path checks the imaginary residual,
keeps the signed real inverse, and measures negative mass before correction.

- `negative_policy="raise"` rejects significant negative intensity.
- `negative_policy="project"` projects onto nonnegative, unit-sum kernels using
  Euclidean simplex projection. It explicitly changes the candidate when needed;
  it does not claim to recover omitted phase or optimize only the measured band.
- Tiny negative roundoff is clipped and normalized. Significant imaginary
  content is always rejected; check Hermitian symmetry and FFT ordering.

The diagnostics include minimum inverse intensity, negative mass and fraction,
imaginary residual, whether projection or roundoff clipping occurred, L1
correction, maximum complex-OTF change, and energy on the kernel boundary. The
final candidate is then checked independently against the supplied curve.

Linear interpolation connects measurements. Above the final measured frequency,
`high_frequency="zero"` truncates and `"hold"` retains the endpoint value. This
choice also covers diagonal corners of the square frequency grid. It is a
declared modeling assumption, not extrapolated measurement evidence.

The legacy optics conversion paths use checked real inversion instead of
reflecting negative intensity with `abs()`. Legacy callers are not equivalent
to the measured-curve workflow: they do not by themselves provide a comparison
against original measurements or establish a measured calibration.

## Measurement and integration boundaries

An optical MTF, RAW-system MTF and processed-image MTF include different effects.
This utility does not separate lens blur, pixel aperture, charge diffusion, CFA,
demosaicing or sharpening. Do not put a complete camera MTF into a lens model and
then apply the same sensor or ISP effects again. Sharpened responses above 1 are
rejected by this nonnegative normalized PSF model.

The output array represents **discrete impulse weights**. It does not silently
integrate a continuous PSF over pixels or add a pixel-aperture transfer function.
It therefore does not automatically satisfy `camera_platform`'s qualified
pixel-integrated PSF asset contract. Conversion into such an asset requires its
own sampling, reference-plane, provenance and qualification checks. Directional
sagittal/tangential measurements, field-varying/asymmetric PSFs and phase recovery
require additional information and are outside this radial input API.
