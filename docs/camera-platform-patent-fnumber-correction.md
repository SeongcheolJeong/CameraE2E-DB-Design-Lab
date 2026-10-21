# Canon p0248: patent F-number and operating-stop correction

The DB design demo uses Canon **US20200174235A1, Numerical Example 1,
wide-angle**. Its source table specifies 24.72 mm focal length and **F/4.12**.
The earlier source-stop demo instead computed F/2.751 at 550 nm. That was a
different operating pupil, not the patent's declared wide-angle condition.

Primary source: [US20200174235A1, Numerical Example 1](https://patents.google.com/patent/US20200174235A1/en).
The patent states: “An aperture stop SP determines (restricts) the beam of the
full aperture F-number (Fno).” It defines the d-line reference as 587.6 nm.

## Cause and correction

The table column is **Effective aperture**. Surface 15, the stop, has value
19.96 mm. The existing import adapter interprets this value as a diameter and
therefore sets a 9.98 mm hard-clear radius. This diameter interpretation is an
explicit modeling assumption; the table does not establish a measured operating
iris diameter or a maximum mechanical aperture.

RayOptics maps the physical stop through the front lens groups to the entrance
pupil. At 587.6 nm the 9.98 mm source-stop radius produces a 4.498382857 mm
entrance-pupil radius: a magnification of 0.450739765. The resulting F-number is
24.720263850 / (2 × 4.498382857) = **2.747683405**. The nominal F/4.12 had previously
been retained as source metadata without controlling this pupil.

The corrected execution prescription derives an operating-stop radius using
the declared d-line F-number:

```text
operating radius = source radius × source F-number / target F-number
                 = 9.98 × 2.7476834051250587 / 4.12
                 = 6.655796209501962 mm
operating diameter = 13.311592419003924 mm
```

This is one fixed physical iris across all wavelengths and fields. The iris is
not independently adjusted to F/4.12 at every wavelength. Its diameter is a
model-derived value, not a value directly obtained from the patent or measured
from a product.

| Condition | Wavelength | EFL (mm) | Entrance-pupil radius (mm) | F-number |
|---|---:|---:|---:|---:|
| Source effective-aperture interpretation | 550 nm | 24.732880403 | 4.495131897 | 2.751073936 |
| Source effective-aperture interpretation | 587.6 nm | 24.720263850 | 4.498382857 | 2.747683405 |
| Derived operating iris | 550 nm | 24.732880403 | 2.997863912 | 4.125083914 |
| Derived operating iris | 587.6 nm | 24.720263850 | 3.000032021 | 4.120000000 |

The geometry provides independent checks: the d-line EFL is 24.720264 mm
against the table's 24.72 mm; entrance-pupil position is 32.226278 mm against
32.23 mm; exit-pupil position is −44.081084 mm against −44.09 mm. These checks
support the prescription translation and do not establish measured product
agreement.

## Preserved source and consistent execution

The original CSV remains unchanged:

```text
camerae2e_db/lens_db/CameraE2E_Lens_DB_v9_20260627/data/lens_patents/
reviewed_prescriptions/canon/US20200174235A1_p248_numerical-example-1/
configured_surfaces/wide-angle.csv
SHA256: b0cfd975001ae7e8ddff2144743cf500031253ce6ff1f160b50d0ac7dfc7c318
```

`reference_workshop_optics._operating_pupil()` derives the new stop, and
`_request()` supplies that same physical prescription to forward geometric PSF
tracing and reciprocal radiometry. Both retain hard-aperture ray rejection.
Neither multiplying an existing PSF's brightness nor changing only the forward
entrance-pupil launch would provide this consistency.

The stored `pupil_condition` records the target F-number, reference wavelength,
original stop, derived operating stop, attained pupil, derivation, source URL,
and lack of physical validation. The applied prescription hash and pupil
condition participate in the optical cache identity. Old source-stop PSFs and
RAW must remain historical results; they are not reused as F/4.12 results.

The last numbered row is also preserved. Surface 36 has radius −56.995 mm and
wide-angle image gap 13.50 mm; the primary table then has a separate Image plane
caption. Although the CSV labels row 36 as `image`, it is a real curved
interface. The importer retains it and omits only terminal, unnumbered image
captions. Dropping surface 36 would corrupt the lens; it did not cause this
F-number mismatch.

## Scope and checks

The first corrected-aperture run was explicitly refused: the old 40 radial ×
28 azimuth reciprocal grid sparsely sampled the narrower off-axis pupil. The
28 × 48 grid reallocates samples toward azimuth and increases total samples
from 1,120 to 1,344 per coarse field/band. Its paired coarse/fine work remains
994,560 interface visits, below the unchanged 1,000,000-visit Factory budget.
No convergence tolerance was relaxed. All 11 wavelength batches passed with
maximum relative-illumination change 0.013376 (limit 0.03), angular-CDF change
0.023774 (limit 0.05), and irradiance relative change 0.013501 (limit 0.03).
Only p0248 uses this grid; the Reference baseline keeps its original grid.
These are observed two-level quadrature differences, not a rigorous error
bound or physical validation.

The correction changes the operating aperture used by the geometric ray PSF,
pupil directions, clipping and reciprocal throughput. It does not add
diffraction, coating data, measured glass dispersion, or physical camera
validation. The effective-aperture interpretation for other surfaces and the
nd/Vd ModelGlass approximation remain explicit assumptions. Existing numerical
convergence checks must still pass for the regenerated result.

`tests/platform/test_db_patent_pupil.py` checks source immutability, d-line
F-number, unchanged geometry, one iris across disjoint wavelength batches,
distinct cache identity and propagation to both solver entry points. Its
dispatch test uses mocked ray outputs only to inspect plumbing and is not
presented as an executed optical result. A newly computed optical/RAW/ISP run
provides the separate demo evidence.

## Executed comparison — 2026-10-13

Completed run: `run-33817fe3ce14497d88aa4ef55e0b4cda`.
Historical source-stop run: `run-ee2e6236772e4e088c085b3ed0b12d1f`.

[Open the same p0248 × IMX462 camera before/after](http://127.0.0.1:8780/db-design.html?run=run-33817fe3ce14497d88aa4ef55e0b4cda&baseline_run=run-ee2e6236772e4e088c085b3ed0b12d1f&candidate-a=candidate-02&candidate-b=candidate-02&field=center&chart=texture&stage=isp).
Select `outer-ne`, another chart, or RAW/ISP stages without recapturing. Each
side retains its own run, optical condition, files and hashes.

The four lens × sensor combinations produced **256 native 256×256 int32 RAWs**,
at D65, 1000 cd/m², 4 ms, gain 1, eight independent captures. The common CCM
matrix is bit-identical to the previous run. Both unchanged Reference-lens
combinations reproduced all 128 previous RAWs bit-for-bit; all 128 p0248 RAWs
changed. Both complete 1,418-file manifests passed verification. The original
CSV and historical manifest hashes are unchanged.

| p0248 × IMX462 | Center, old → F/4.12 | Outer-ne, old → F/4.12 |
|---|---:|---:|
| Mean photoelectrons | 1043.997 → 467.221 | 853.059 → 442.254 |
| Green input-referred temporal RAW noise (e⁻ RMS) | 37.781 → 26.350 | 34.406 → 25.758 |
| Geometric radial MTF at 0.25 cycles/native pixel, receiver aperture included | 0.627 → 0.951 | 0.273 → 0.581 |
| Processed SFR50 (cycles/native pixel) | 0.116 → 0.244 | 0.061 → 0.173 |

The aperture correction trades photon signal for reduced geometric blur. A
smaller absolute shot-noise value does not establish better SNR. The MTF here
excludes diffraction, and SFR includes the fixed ISP; neither is a measured
product-lens result. The mean signal retains 44.75% of the old center value and
51.84% at outer-ne, so the display intentionally stays darker at fixed exposure.

The executed calculation/storage took 378.62 seconds including cold optical
recalculation, and the package is 571,330,382 bytes. The first refused run remains
recorded as failed; it is not a completed comparison. Evidence and the local
verification entry point are in `outputs/db-design-fnumber-20261013/`:
`delivery.json`, `verification.json`, `verify_delivery.py`, and the per-band
`radiometry-28x48-spectral/` summaries/arrays. The scoped regression suite passed
62 tests, and the DB comparison browser fixture passed.
