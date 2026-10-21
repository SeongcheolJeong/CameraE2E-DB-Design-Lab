# Sampled CDS and native row/column read noise

Development-source contract `camera_capture_program_v3` is an opt-in, registered
independent-exposure RAW/ISP path. It retains the linear electrical model
`camera_electrical_readout_v1` and adds a separately frozen
`camera_cds_correlated_read_noise_v1` profile. The existing v1 program and the
nonlinear `camera_electrical_readout_v2` keep their identifiers and behavior.
The capture-program source/normalized blobs, Test, runtime result and ZIP bind
the profile through the registered program content hash.

The profile's reference plane is the **read-node input-referred electron**.
Photocharge and dark charge are generated and well-limited upstream. For each
fresh read, the synthetic circuit takes a reset sample, then a signal sample,
then subtracts reset from signal. One pixelwise reset-node term is present in
both samples and cancels exactly in the algebraic CDS residual. Each sample also
has pixel, native-row and native-column Gaussian terms. The two samples for
each component have declared RMS values and a correlation coefficient from
−1 to +1. Row terms are shared by all sites on a native row, and column terms
by all sites on a native column. Random streams draw on the full native sensor
before crop, with separate per-frame seeds. Cropping therefore retains native
read-noise site identity for a given frame seed. The physical row timing of
these sample operations is **not** modeled; the exposure/transfer timing still
comes from the existing sequential capture contract.

`read_noise_e` and `post_gain_noise_v` in the base linear model must both be
zero for this program. They are aggregate noise values in legacy programs;
adding the CDS profile to them would count noise twice. The profile declares a
closed temperature interval and authored provenance. Outside that interval the
run is refused. Noise disabled leaves all temporal CDS arrays zero while
fixed-pattern maps and ADC rounding remain active.

The result retains reset-sample noise, signal-sample noise and their algebraic
CDS residual as electron arrays alongside integer ADC/RAW, per-frame ISP and
native geometry. Calibration reports the **per-pixel marginal** CDS variance,
`Σ(σ_reset² + σ_signal² − 2ρσ_resetσ_signal)`, including row/column components.
It is not an independent-pixel noise model: two sites in the same row or
column share a random component. The existing HDR merge uses independent-pixel
scalar variance weights, so v3 refuses `hdr_merge` rather than using those
weights with false covariance assumptions. It supports single or multiple
sequential frames and same-RAW ISP processing without fusion.

`GET /api/v1/templates/capture-program?...&mode=independent_bracket_cds`
provides a visibly authored example. Its exact numeric RMS and correlation
values are **not sensor measurements**. The captured result is a computational
implementation check; physical validation and total uncertainty are unknown.
Bandwidth, 1/f noise spectrum, circuit reset settling, CDS aperture, correlated
cross-frame noise, shared-charge HCG/LCG, traps, nonlinear response, conversion
gain switching and hardware ADC defects are not represented. Native crop
identity applies to this read-noise stream, not to every stochastic upstream
process (in particular, shot-noise samples across different crop sizes are not
guaranteed to match). The implementation generates full-native Gaussian maps
for the pixel terms, so the HDR runtime includes their memory in its bounded
resource preflight.
