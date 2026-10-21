# Moving 3D scene, PWM, ambient, and retained paired RAW

This bounded implementation experiment connects the existing registered
modules in `tests/platform/test_registered_3d_pwm_trapped.py`. A black mesh
translates across a lit background in a real Mitsuba 3D Scene. Five immutable
render jobs at 0, 20, 40, 60, and 80 ms produce one registered spectral
scene sequence. A declared 1 ms / 450 µs PWM waveform gates that sequence on
each rolling row. A separate, constant ideal-image ambient component remains
ungated. A v4 trapped gain-merge program sends the combined photons through
one causal free/trapped photodiode state and the same delivered charge through
HCG and LCG. The Test, SDK submission, Worker, original integer RAW, ISP
previews, cold-state code-domain ISP replay, and evidence bundle are exercised.

The ambient component is an **authored sensor-plane spectral image**, not a
second separately rendered 3D light. This is a deliberate contract boundary:
the scene renderer currently binds a scene illumination identity to its camera
assembly, so an independently lit 3D rendering cannot be introduced as a
second component without a broader scene/light contract. The experiment does
not infer ambient from RAW. Its spectral irradiance and relative clock are
declared inputs. PWM is an analytic modulation applied after the 3D optical
knot renders; the renderer itself does not advance an emissive LED geometry or
resolve within-period light transport. The 3D knot spacing cannot bound fast
occlusion between knots. Current temporal/HDR parsers also use the legacy
separable lens PSF/relative-illumination path: any opt-in source-position RI
that is supported only by the spatial runtime is outside this experiment.

The oracle deliberately starts from the *saved optical electron-rate knots*.
For selected Bayer sites, it integrates the piecewise-linear rate only over
enumerated PWM ON intersections of the actual row windows, then integrates the
ungated ambient rate over the full exposure. It checks each component and
their sum against the saved electron arrays. It next walks the saved ordered
gap/exposure intervals, verifies that scene knots and PWM edges are not crossed,
and independently applies generation, dark current, PD clear, bounded trap
release/capture steps, partial transfer, and HCG/LCG nearest-even ADC and
digital codes. The two phase runs must change the modulated electrons and
original RAW/RGB while leaving the ungated ambient electrons fixed. A new
service instance resolves both original readout streams, and the separate ISP
replay consumes selected integer HCG RAW. Bundle artifact hashes are checked
against their stored bytes.

This oracle checks chronology and electrical encoding independently of the
product integration and charge solvers; it does not independently solve the
3D light transport, PSF, pixel response, or absolute radiometric calibration.
Noise is disabled for a deterministic numerical comparison. The trapped model
is one authored reservoir with bounded split steps, not an exact continuous
CTMC or fitted silicon trap model. HCG/LCG gain-path merge estimates delivered
total charge, not the current exposure's photocharge or multi-exposure scene
radiance. No motion-compensated RAW HDR fusion or measured automotive LFM
performance is established. The run must retain `physical_validation =
not_established`; a passing experiment certifies only this registered
implementation chain and its declared numerical fixture.

On the `0.16.1.dev29` source, this isolated registered command passed both
tests:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src OPENBLAS_NUM_THREADS=1 .venv-platform/bin/python -m pytest -q -p no:cacheprovider tests/platform/test_registered_3d_pwm_trapped.py
```

The 0-to-350 µs PWM phase change altered 298 of 1,152 integer HCG/LCG RAW codes
(maximum 5 DN), while all 1,728 ambient electron entries remained identical.
Trapped occupancy changed in all 576 native pixel/frame entries. These are
fixture-specific observations, not a physical accuracy or LFM bound.
