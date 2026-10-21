# Next priority: measured nonlinear RAW calibration boundary

Proposal after the dev11 retained simulation RAW bridge. This is unimplemented scope, not a completed capability. Preserve general optics/pixel/electrical, HDR/LFM and advanced ISP/optimization priorities and the original seven modules/four execution modes.

The immediate gap is that advanced nonlinear replay only accepts simulation runs. Add a separately versioned measured nonlinear RAW import contract carrying byte layout, CFA/native crop, exposure/gain/temperature, timing, explicit DCG path/acquisition identity and a fixed response calibration. Preserve raw bytes and measurement provenance. Do not manufacture unknown calibration fields from simulation defaults.

Absolute response fitting is not identifiable from DN and exposure alone. A fit must declare its independent calibrated charge/radiometric/response anchor and uncertainty, or remain a relative response model that cannot enter the electron-domain path. Importing a manufacturer's declared response with explicit origin is distinct from fitting a measured response and from validating it.

Before implementation, specify usable calibration observations, monotonic domain/knots, read-noise plane, black/dark separation and clipping/censoring. Enforce training/validation separation by acquisition lineage, including paired DCG reads. Freeze candidate fit versions and compare predictions to held-out RAW code distributions with measurement and model uncertainty kept distinct. A fit should report residuals, coverage and unresolved identifiability instead of one unqualified accuracy score.

Acceptance should first prove calibrated synthetic recovery with an independent forward construction, reject unanchored absolute claims, preserve original bytes/geometry/settings, and reproduce held-out residuals. Physical-camera accuracy remains unestablished until real measurements with known provenance and uncertainty are supplied. Optics field/depth/polarization/coating behavior, charge transport dynamics, general motion/PWM HDR and adaptive multi-objective optimization remain additional open model work.
