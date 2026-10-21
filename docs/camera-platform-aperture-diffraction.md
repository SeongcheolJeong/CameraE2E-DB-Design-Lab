# Aperture comparison with prescription aberration and diffraction

The `aperture_diffraction` DB Design Lab recipe compares the historical source-stop
condition of Canon US20200174235A1 numerical example 1 with the patent F/4.12
condition. Both are recomputed using the same `exit_pupil_diffraction` profile.
Historical geometric results remain readable and are not relabeled as diffraction.

## Fixed conditions

- The same 36-surface prescription, sensor plane, materials hypothesis, sensor
  response, exposure, gain and common ISP are retained.
- The source-stop condition has a modeled F/2.751 at 550 nm. Its 19.96 mm stop
  diameter is an interpretation of the patent's effective-aperture column.
- The patent condition uses one physical stop, radius 6.655796 mm, derived to
  reproduce F/4.12 at the patent's 587.6 nm reference wavelength. Its F-number at
  550 nm is approximately 4.125. Neither condition rescales its stop by wavelength.
- Spectral PSFs are actually calculated at 450–650 nm in 10 nm increments, at
  center and outer-ne. Both cameras retain 2.9 µm native pixels and 256×256 probes.
- The outer-ne node uses the same object direction for both apertures. Its
  spatial response is a local shift-invariant approximation within the probe.

## Why a separate propagation profile is necessary

The existing `wavefront` factory assumes a paraxial exit-pupil map and an image
plane perpendicular to its reference axis. This lens's outer field exceeds that
factory's 2° reference-axis limit; the source-stop center also exceeds its ray-cone
limit. Those limits have not been raised. The new profile instead uses traced
reference-sphere coordinates and a scalar angular spectrum evaluated on the
actual flat sensor plane.

1. RayOptics traces the authored prescription, hard apertures and stop. The
   physical source angle is fixed. The actual **550 nm chief image position**
   defines a shared reference point at the authored sensor plane.
2. Every admitted ray is intersected with the near side of that reference sphere.
   Its direction coordinate is computed from the intersection to the shared
   image reference. This is not the aberrated outgoing ray direction.
3. RayOptics supplies chief-minus-ray optical path difference in nm at that
   sphere. The complex pupil uses `exp(-2π i OPD / wavelength)`. No focus term,
   lateral color or chromatic displacement is removed.
4. The entrance-pupil area to direction-coordinate area Jacobian transports
   uniform incident scalar flux. Folded maps are rejected. Connected triangles
   cannot bridge unresolved clipped gaps. Extra traced boundary rays locate the
   hard pupil edge independently of the interior grid.
5. Prysm's fixed-sampling Fourier transform evaluates the scalar direction
   spectrum on the sensor plane. Unwrapped OPD is interpolated before forming
   complex phase; wrapped complex phase is never interpolated.
6. Receiving subpixels are integrated into each 2.9 µm pixel exactly once. The
   conditional PSF is normalized after checking retained energy. Physical +Y is
   converted to decreasing image rows once.

Absolute irradiance remains the separate reciprocal radiometry calculation using
the **same physical stop**. It is applied once, followed by the existing absolute
pupil-averaged sensor response. Thus stopping down changes aberration, diffraction
and photon rate together; RAW gain or display normalization cannot conceal the
light loss. A geometric ray histogram is not convolved with an Airy kernel.

## Numerical acceptance and cache identity

Each field/wavelength node separately checks:

| Check | Required condition |
| --- | --- |
| Physical pupil refinement | Initial 48→96 rays per lattice axis; retry 64→128 and 96→192 if needed |
| Direction grid refinement | Automatically sized grid and twice that grid |
| Receiver integration | 5 versus 11 subpixels per native-pixel axis |
| Normalized kernel difference | L1 ≤ 0.03 across the independent refinements |
| Retained conditional energy | ≥ 0.95 and ≤ 1.001 |
| Unwrapped OPD interpolation | Independent fine-ray maximum error ≤ 0.02 waves in overlap |
| Final complex-phase grid | Neighbor OPD difference ≤ 0.25 waves |

Fine rays outside the coarse interpolation hull are recorded as a coverage
fraction; their effect is included in the complete PSF refinement comparison.
No interpolation-error gate is inferred solely from a denser interpolated grid.
The blue wavelengths have substantial chromatic defocus and can require the
64→128 physical-ray check even when the 550 nm node passes 48→96.

Failure at the maximum declared refinement refuses the node and prevents RAW
generation. There is no fallback to a geometric kernel and no automatic
tolerance relaxation. The cache key includes source and applied prescriptions,
the complete optical specification, reference anchor, exact wavelength, field,
and implementation/backend hashes. Each completed node is independently hashed
and can be resumed. Existing geometric caches are preserved separately.

## What the three optical curves mean

The result stores three 550 nm receiver-integrated, native-sampled PSF transfer functions:

- `geometric`: the same physical aperture's geometric landing histogram.
- `aberrated_diffraction`: the traced OPD plus scalar diffraction actually used
  for the new RAW and ISP images.
- `diffraction_limited`: a diagnostic with OPD set to zero on the **same admitted
  pupil and scalar flux distribution**. This isolates the aperture's diffraction
  penalty. It is not a claim about a separate ideal circular lens.

These curves are discrete transforms of the native sampled impulse response,
including receiver-pixel integration. They are **not continuous lens-only MTF**:
source-cell-center sampling can alias an optical impulse whose bandwidth exceeds
the native grid. In particular they do not establish continuous scene accuracy
up to sensor Nyquist. Processed image SFR remains a separate quantity after the
chosen demosaic, color and display operations.

## Scope and validation

This is a scalar model with uniform incident entrance-pupil flux. It does not
solve vector diffraction, polarization or coating-amplitude transport. The sensor
angular response remains a separable pupil average, not joint coherent
landing-position/direction transport. Glass dispersion and effective apertures
retain their source-derived modeling assumptions. No product or physical-camera
validation follows from these numerical checks.

Independent tests compare the propagation with analytic Bessel/Airy receiver-bin
integrals, impose signed X/Y phase ramps, check the flux Jacobian and a clipped
pupil hole, and compare a physically traced fixed-sensor PSF centroid with ray
landing positions. The existing geometric and patent-pupil tests remain in use.

Relevant implementation:
`reference_exit_pupil.py`, `reference_workshop_optics.py`, `db_design_optics.py`;
tests: `test_reference_exit_pupil_independent.py` and
`test_reference_exit_pupil_integration.py`.

## Completed demonstration, 2026-10-17

Run `run-a3c36ff4c9d24b39966a929836e107ae` contains the same-p0248 aperture pair
as candidates 01/02 and the common-color reference as candidate 00. All 84
diffraction nodes passed the declared gates; maximum kernel L1 difference was
0.0161598, minimum retained energy 0.9973247, and maximum independent OPD
interpolation error 0.0181074 waves. Four blue open-aperture nodes needed the
64→128 ray refinement; the other nodes passed 48→96.

Both apertures use the same 4 ms exposure, gain 1, fixed ISP/display and eight
independent captures. Stopping down reduced mean photoelectrons from 1044.00 to
467.22 at center and from 853.06 to 442.25 at outer-ne. Processed SFR50 changed
from 0.11934 to 0.21101 and from 0.04801 to 0.15828 cycles/native pixel,
respectively. These are chart/system values, not lens-only MTF.

The 192 native integer RAWs (128 aperture-pair plus 64 reference), 1,232-file
manifest, original source CSV, and preserved historical manifests were verified.
The new geometric diagnostics and uniform photon rates match the corresponding
historical aperture runs, isolating this update from a prescription/radiometry
change. See [the prepared demo and delivery evidence](camera-platform-db-design-lab.md)
in the final dated section of the DB Design Lab documentation and
`outputs/db-design-diffraction-20261017/verification.json` for exact values.

## Stepped iris study and reciprocal radiometry

The `aperture_sweep` recipe uses the same prescription and authored sensor plane
at F/2.75 (the preserved historical source stop), F/3.2, F/4.12, F/5.6, F/8 and
F/11. A custom sorted list of two to eight unique values within this range is
also accepted. F/4.12 retains its patent-derived condition. Intermediate and
narrower settings are explicitly `user_design_setting` operating irises, not
additional conditions claimed by the patent. Each physical stop is derived at
587.6 nm and shared by all forward PSFs and reciprocal irradiance calculations.

Narrow irises expose a quadrature limit in the original bounded Factory. The
full rear-plane disk is much larger than the transmitted pupil, so the old
grid can have too few admitted rays. `db_design_radiometry.py` adds a separate
explicit resource profile; it does not modify the Factory's public limits or
acceptance thresholds. It first compares 128×256 with 256×512 rays per field
and wavelength. If that fails, it compares 256×256 with 512×512. Both attempts
and their differences are recorded. The upper work bound is 1,966,080 rays and
100 million surface visits per two-wavelength/two-field batch, with a 180-second
deadline. The complete equal-area enclosing disk, actual stop transmission,
and `cos(theta)^4 / distance^2` irradiance weights remain unchanged.

Every successful batch requires positive throughput, no invalid trace geometry,
relative irradiance change ≤3%, relative-illumination absolute change ≤0.03,
and angular-CDF change ≤0.05. Exhausting the ladder or deadline is a refusal;
it does not weaken these thresholds. The F/8 blue pilot exercised the retry:
the first grid failed at 3.588% relative irradiance change, and the larger pair
passed below 1.980%. This is observed numerical agreement, not a rigorous error
bound or physical validation.

Adaptive batches may use different direction grids. Their actual directions
are therefore preserved per wavelength, with zero-weight padding only; no
positive photon weight is dropped or regridded. The current pinned pixel model
is explicitly azimuth independent. On-axis annular quadrature is not a general
substitute for a sensor's azimuth-dependent response. Original runs remain
immutable, while the new numerical profile and helper hash enter cache identity.
