# Registered temporal/HDR source and receiver ROI

An immutable `spectral_image` stores the **source support** at its native
`sampling.crop_origin` and `shape`. A temporal or `hdr_bracket_v1` run may
capture a smaller **receiver ROI** without resampling or changing that source
artifact. The runtime convolves the whole supplied source support, routes
generated charge on that support when the Sensor declares collection, and
then selects `capture_crop` for row integration, RAW and ISP. Source-addressed
carrier ledgers retain the source shape; output RAW and frame arrays use the
receiver shape.

The Test template accepts four query fields together:
`capture_row`, `capture_col`, `capture_height`, `capture_width`. The resulting
Test stimulus freezes `capture_crop` and the source-support geometry. The run
must supply the same `parameters.capture_crop`, or use a halo-rendered image
whose immutable render record pins that receiver ROI. An explicit receiver
that differs from a halo render is refused. For sources without a frozen halo
render, a Test without a receiver crop keeps its prior full-source stimulus
and content identity.

For example, with a registered 50×50 source at native origin `[39,39]` and
an 8×8 receiver at `[61,59]`:

```python
crop = {"crop_origin": [61, 59], "shape": [8, 8]}
spec = sdk.template(
    "temporal-test", assembly_id=assembly_id,
    scene_sequence_id=scene_id, control_sequence_id=control_id,
    capture_program_id=program_id,
    capture_row=61, capture_col=59, capture_height=8, capture_width=8,
)
test = sdk.post("tests", spec)
run = sdk.submit_hdr(
    assembly_id, test["id"], scene_id, control_id, program_id,
    capture_crop=crop,
)
```

`sdk.submit_temporal(..., capture_crop=crop)` uses the same registered source
and Test binding without the electrical/HDR capture program. For two-component
HDR, `unmodulated_scene_sequence_id` remains a second immutable registered
source. Both components must share clock, wavelength, units, native sampling,
source shape and receiver ROI. Their source IDs, content hashes and artifacts
remain separate; no combined scene or inferred ambient component is created.

For a shared-charge DCG program, `sdk.submit_dcg(..., capture_crop=crop)`
submits the same frozen receiver geometry through the registered API and
retains both HCG and LCG RAW streams.

Preflight checks the PSF source halo, and when charge collection is present,
the combined PSF and carrier-source halo, inside the physical sensor. It
rejects missing source pixels rather than filling them. The registered
control sequence is still validated when created against the source shape;
run-time row windows and output geometry use the receiver ROI. A worker
reconstructs the request from registered sequences, model revisions and the
frozen Test before capture. The optical/electrical runtime does not use the
Test's internal truth as an input.

For a cropped linear temporal or HDR Test, the prediction and an external
measurement must each declare `sample_geometry` with the exact frozen
`capture_crop` and `source_support`. Missing or different coordinates make the
comparison invalid even when channel values and frame times happen to match.
The measurement declaration establishes comparison coordinates only; it does
not prove a measured optical halo or physical accuracy.

Development integration tests execute published 3D renders through registered
scene/control/Test and HDR Worker to 8×8 integer RAW and RGB. One pins a PSF
halo; another pins a directional Lens, matched PSF, native generation response
and asymmetric charge-collection halo together. The latter checks that the
source-support generated rate equals collected rate plus intrinsic,
off-sensor and outside-support losses. A derivative image cut down to the
receiver is refused because the combined halo is missing. A requested crop
that conflicts with a render's frozen receiver is also refused. These are
synthetic software and local Worker checks, not measured optical accuracy.

The supported registered topologies here are `temporal_scene_v1`,
`hdr_bracket_v1`, `shared_charge_dcg_v1`, and retained-charge v1/v2/v3/v4.
A registered DCG ROI preserves
the full spectral source support, paired HCG/LCG readouts, CFA phase and
native row clock. A local Worker comparison against a full-native capture
confirmed matching mean charge and deterministic dark-map values at the same
sensor coordinates, plus two nonidentical integer RAW paths. Identical
same-seed integer RAW values across full-frame and cropped captures are not
part of this contract: stochastic draws follow each run's raster order. The
registered retained-charge ROI preserves one fixed native receiver state and
row-event chronology across the complete sequence. Its full-sensor dark/PRNU
maps are sliced at the native origin, and the native R/Gr/Gb/B transfer/trap
parameters use that origin. Selected integer RAW can be replayed through the
registered causal code-domain ISP with the same origin-aware Bayer phase and
retained row-event contract. The source image still supplies the required PSF
and carrier halo. Retained photo-HDR v5 inverse, reference-HDR and
electrical-control topologies still do not accept this ROI binding. The retained
ROI can be selected through the template query and SDK/API; the Workbench
currently has no receiver-crop form. Temporal control registration still
applies its source-sized frame-pixel budget, even when the eventual receiver
is smaller. This is a software calculation and evidence contract; physical
accuracy and measured halo fidelity remain unestablished.

The retained-state ROI is not a model of readout-window switching inside a
sequence. Pixels outside the fixed receiver are omitted only because this
particular PD model has independent per-pixel reservoirs. A different model
with cross-pixel blooming, trap transport or shared bias would need those
interactions on the full sensor. With noise disabled, the ROI's state and RAW
match the same native slice of a full-sensor execution. Stochastic ROI and
full-frame runs use different raster RNG draws, so byte-for-byte sample
identity is not promised; the per-pixel distribution is the same authored
model. Cropped row events and state hashes bind the original sensor geometry.
