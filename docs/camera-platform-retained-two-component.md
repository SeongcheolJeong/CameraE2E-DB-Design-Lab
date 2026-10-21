# Retained-charge PWM LED and unmodulated ambient

The retained-charge runtime accepts two **authored** ideal-image spectral scene
sequences for capture programs `camera_retained_charge_capture_program_v1` through
`camera_retained_charge_capture_program_v4`. Versions v1/v3 are trap-free;
v2/v4 use one authored free-charge and trapped-charge population. The primary
`scene_sequence_id` is the light affected by the program's explicit PWM waveform. The optional
`unmodulated_scene_sequence_id` is light that remains on independently of that
waveform. Omitting the second ID keeps the existing single-scene request, arrays,
summary, and run identity unchanged.

For each native sensor row, the model integrates the two optical rates against
their own declared linear or zero-order-hold knot timelines:

```text
LED photoelectrons     = integral(LED rate(t) * PWM(t), row exposure)
ambient photoelectrons = integral(ambient rate(t), row exposure)
PD input photoelectrons = LED photoelectrons + ambient photoelectrons
```

The two integrals are summed **before one photodiode population evolves**. For
v2/v4, the runtime splits row exposure at both scene timelines' knot boundaries,
PWM edges, and bounded trap steps. It adds LED and ambient photoelectrons within
each short interval before the same free/trapped state evolves; it never sums
two separately delivered RAW frames. Existing PRNU, shot sampling, capacity,
partial transfer, dark generation, and electrical readout then act on the
combined population. In v3/v4, HCG and LCG read the same
delivered charge. This is a gain-path delivered-charge merge, not an inverse
estimate of the current scene or a multi-exposure HDR reconstruction. Changing
PWM phase cannot change the ambient integral, but it can change LED charge and
therefore the total RAW and later retained history.

Both sequences need the same capture clock, native geometry, optical sampling,
wavelengths, units, reference plane, and declared upstream effects. The Test
stimulus `retained_charge_components_test_v1` pins both registered IDs and
content hashes. A run freezes both sequences and their image artifacts. The
Worker reconstructs its request from those snapshots; code-domain ISP readback
checks the original second sequence, optical summary, authored trap chronology
for v2/v4, and artifact provenance before accepting the selected delivered RAW
path. Source arrays expose each component's row weights, optical rates, and electrons, while `channel_electrons`
is their sum. Collection-rate evidence from either source counts against the
bounded output budget before optical execution.

```python
test = client.post("tests", client.template(
    "temporal-test",
    assembly_id=assembly_id,
    scene_sequence_id=led_scene_id,
    unmodulated_scene_sequence_id=ambient_scene_id,
    control_sequence_id=control_id,
    capture_program_id=program_id,
))
run = client.submit_retained_charge(
    assembly_id, test["id"], led_scene_id, control_id, program_id,
    unmodulated_scene_sequence_id=ambient_scene_id,
)
```

The program must have an explicit waveform. Dual input is rejected for
photo-HDR v5; a causal inverse of current exposure photocharge from delivered
trapped history is not defined. The v2/v4 trap calculation is a bounded
first-order split approximation, not exact trap chemistry, CTMC, or measured
device calibration. Versions v1–v4 support one fixed, registered receiver ROI
inside both sources' native support. The receiver keeps native row timing, CFA
phase, and one shared retained state; cold ISP readback checks the frozen ROI
against both optical components. With noise enabled, the same RNG seed on full
and cropped runs does not reproduce identical noise samples at overlapping
pixels, although their deterministic means and fixed patterns match. The current
browser form can find and show this Test schema, but it does not yet author the
second sequence input.

Validation compares **total delivered RAW DN** against declared observations.
Neither Test validation nor ISP readback infers separate LED and ambient
contributions from RAW. The split, source spectra, PWM timing, photodiode
parameters, and optical transfer are authored simulation inputs. Passing these
software checks does not establish a measured LED waveform, sensor calibration,
physical LFM performance, or a commercial release claim. Independent arithmetic,
registered v1/v3 Worker, source tampering, variant gates, and legacy identity are
covered by `tests/platform/test_retained_two_component.py` and
`tests/platform/test_retained_trap_two_component.py`.
