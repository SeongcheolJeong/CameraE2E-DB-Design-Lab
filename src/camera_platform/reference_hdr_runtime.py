"""Independent electrical exposures with explicit reference-constrained RAW fusion.

The existing HDR acquisition kernel is invoked with fusion disabled. Existing
static-bracket contracts and pixels remain unchanged. The opt-in translation
profile estimates bounded shifts from the acquired RAW alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

import numpy as np

from camera_platform.runtime import with_sensor_effects

from . import hdr, hdr_runtime, reference_hdr
from .runtime import RuntimeRequestError, _issue

PLUGIN_ID = "reference_hdr_v1"
PROGRAM_SCHEMA = "camera_reference_hdr_capture_program_v1"
NONLINEAR_PROGRAM_SCHEMA = "camera_reference_hdr_capture_program_v2"


def _plain_program(program):
    """Translate only the wrapper schema; preserve explicit v1/v2 model meaning."""
    return {
        **program,
        "schema": (hdr_runtime.NONLINEAR_PROGRAM_SCHEMA
                   if program["schema"] == NONLINEAR_PROGRAM_SCHEMA
                   else hdr_runtime.PROGRAM_SCHEMA),
        "hdr_merge": None,
    }


def validate_capture_program(payload):
    if not isinstance(payload, Mapping):
        raise ValueError("Reference capture program requires an object")
    nonlinear = payload.get("schema") == NONLINEAR_PROGRAM_SCHEMA
    required = {"schema", "electrical_model", "waveform", "hdr_merge"}
    if nonlinear:
        required.add("preview")
    if set(payload) != required:
        raise ValueError("Reference capture program fields differ from its declared schema")
    if (not isinstance(payload["schema"], str)
            or payload["schema"] not in {PROGRAM_SCHEMA, NONLINEAR_PROGRAM_SCHEMA}):
        raise ValueError("Unsupported reference capture program schema")
    plain = hdr_runtime.validate_capture_program(_plain_program(payload))
    if plain["waveform"] is not None and plain["waveform"]["kind"] != "constant":
        raise ValueError("Reference HDR supports constant illumination only; PWM requires "
                         "the separate no-fusion diagnostic capture")
    profile = reference_hdr.validate_profile(payload["hdr_merge"])
    from . import nonlinear_hdr

    expected = nonlinear_hdr.SCHEMA if nonlinear else hdr.SCHEMA
    if profile["base_profile"]["schema"] != expected:
        raise ValueError("Reference HDR profile must match the capture program version")
    if nonlinear and profile["schema"] == reference_hdr.TRANSLATION_SCHEMA:
        raise ValueError("Global-translation HDR currently requires linear RAW calibration")
    return {**plain, "schema": payload["schema"], "hdr_merge": profile}


def default_capture_program(legacy_readout):
    program = hdr_runtime.default_capture_program(legacy_readout)
    program.update(schema=PROGRAM_SCHEMA, hdr_merge=reference_hdr.default_profile())
    return validate_capture_program(program)


def capture_effects(program):
    selected = validate_capture_program(program)
    return [*hdr_runtime.capture_effects(_plain_program(selected)),
            ("reference_constrained_raw_global_translation_merge"
             if selected["hdr_merge"]["schema"] == reference_hdr.TRANSLATION_SCHEMA
             else "reference_constrained_raw_merge")]


def _parse(request):
    if not isinstance(request, Mapping) or "capture_program" not in request:
        raise ValueError("Reference HDR requires an explicit capture_program")
    program = validate_capture_program(request["capture_program"])
    parsed = hdr_runtime._parse({
        **request,
        "capture_program": _plain_program(program),
    })
    count = len(parsed["control"]["frames"])
    if count < 2:
        raise ValueError("Reference HDR requires at least two independently reset exposures")
    if program["hdr_merge"]["reference_frame_index"] >= count:
        raise ValueError("reference_frame_index is outside the actual control sequence")
    if count * int(np.prod(parsed["base"]["size"])) > hdr.MAX_FRAME_PIXELS:
        raise ValueError("Reference HDR RAW bracket exceeds its total frame-pixel budget")
    translated = program["hdr_merge"]["schema"] == reference_hdr.TRANSLATION_SCHEMA
    if translated:
        if parsed["control"]["mode"]["shutter"] != "global":
            raise ValueError("RAW global-translation HDR requires global shutter")
        height, width = parsed["base"]["size"]
        registration = program["hdr_merge"]["registration"]
        radius = registration["max_shift_px"]
        if ((height - 2 * radius) * (width - 2 * radius)
                < registration["minimum_common_pixels"]):
            raise ValueError("RAW global-translation HDR has insufficient native interior")
        shifts = (radius + 1) ** 2
        if count * height * width * shifts > reference_hdr.MAX_REGISTRATION_WORK:
            raise ValueError("RAW global-translation HDR exceeds bounded comparison work")
    # Base capture's fixed 512 bytes/frame-pixel includes its old merge budget;
    # nevertheless explicitly budget the additional guard and retained masks.
    additional = count * int(np.prod(parsed["base"]["size"])) * (128 if translated else 64)
    parsed["resources"]["working_array_bytes_estimate"] += additional
    if parsed["resources"]["working_array_bytes_estimate"] > hdr_runtime.MAX_WORKING_ARRAY_BYTES:
        raise ValueError("Reference HDR exceeds bounded working array memory")
    parsed["reference_program"] = program
    return parsed


class ReferenceHDRRuntime:
    plugin_id = PLUGIN_ID

    def describe_capabilities(self):
        capabilities = hdr_runtime.HDRBracketRuntime().describe_capabilities()
        capabilities.update(
            plugin_id=self.plugin_id,
            mode="reference_constrained_hdr",
            supports=[
                "constant_illumination",
                "electrical_raw_readout",
                "native_rolling_or_global_exposure",
                "independent_sequential_bracket",
                "changing_declared_image_timeline",
                "reference_constrained_raw_fusion",
                "raw_disagreement_reference_fallback",
                "opt_in_global_shutter_raw_even_pixel_translation",
                "per_frame_configured_isp",
                "native_four_site_absolute_response",
            ],
            unsupported=[
                "general_motion_alignment", "complete_deghosting", "clipped_reference_recovery",
                "pwm_fusion", "vendor_lfm", "separate_ambient_light", "shared_charge_dcg",
                "physical_validation", "automatic_exposure_control",
            ],
        )
        return capabilities

    def validate_request(self, request):
        try:
            _parse(request)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return getattr(exc, "issues", [_issue("request", str(exc))])
        return []

    def _checked(self, request):
        try:
            return _parse(request)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise RuntimeRequestError(
                getattr(exc, "issues", [_issue("request", str(exc))])
            ) from exc

    def estimate_resources(self, request):
        return self._checked(request)["resources"]

    def execute(self, request):
        parsed = self._checked(request)
        try:
            return self._execute(parsed)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            raise RuntimeRequestError(
                getattr(exc, "issues", [_issue("request", str(exc))])
            ) from exc

    def _execute(self, parsed):
        output = hdr_runtime.HDRBracketRuntime()._execute(parsed)
        arrays, summary = output["arrays"], output["summary"]
        program = parsed["reference_program"]
        exposures = [frame["applied"]["exposure_ns"] / 1e9
                     for frame in parsed["control"]["frames"]]
        calibrations = [{
            **{key: value for key, value in calibration.items()
               if key not in {"dark_e_array", "frame_index"}},
            "dark_e": arrays["calibration_dark_e"][index],
        } for index, calibration in enumerate(summary["calibrations"])]
        merged = reference_hdr.merge_reference_raw_bracket(
            arrays["raw_frames"], exposures, calibrations,
            geometry=summary["geometry"], profile=program["hdr_merge"],
        )
        index = program["hdr_merge"]["reference_frame_index"]
        merged["summary"]["reference_frame_id"] = summary["frame_ids"][index]
        arrays.update({"hdr_" + key: value for key, value in merged["arrays"].items()})
        arrays["rgb"] = merged["arrays"]["rgb"].copy()
        if any(array.nbytes > hdr_runtime.MAX_OUTPUT_ARRAY_BYTES for array in arrays.values()):
            raise ValueError("Reference HDR output exceeds the per-array storage bound")
        effects = with_sensor_effects(capture_effects(program), parsed["base"])
        summary.update(
            plugin_id=self.plugin_id,
            engine="spectral PSF/RI/QE + analytic constant-light exposure + "
                   "electrical RAW + reference-constrained fusion",
            capture_program=deepcopy(program),
            included_effects=effects,
            hdr_merge=merged["summary"],
            hdr_merge_applied=True,
            main_preview="hdr_rgb",
            limitations=[
                "Independent reset/readout exposures; no shared-charge DCG or covariance.",
                "Only constant illumination is accepted; varying PWM remains "
                "no-fusion diagnostics.",
                "Reference-invalid pixels remain unknown; no bright-region headroom recovery.",
                ("RAW-derived even-pixel global translation is conditional and cannot prove "
                 "local correspondence or deghosting."
                 if program["hdr_merge"]["schema"] == reference_hdr.TRANSLATION_SCHEMA else
                 "RAW agreement is conditional evidence, not motion correspondence "
                 "or registration."),
                "Disagreement uses a fixed reflected 3x3 guard and reference-only fallback.",
                "Exposure integration can contain motion blur; linear image knots are crossfades.",
                "Field PSF and angular response remain separable supplied optical approximations.",
                "Per-frame configured ISP and merged HDR preview are separate processing paths.",
                "No physical validation, total uncertainty, deghost guarantee or vendor LFM.",
            ],
        )
        if program["schema"] == NONLINEAR_PROGRAM_SCHEMA:
            summary["limitations"] = [
                item for item in summary["limitations"]
                if item != ("Per-frame configured ISP and merged HDR preview "
                            "are separate processing paths.")
            ] + ["Per-frame preview decodes RAW and uses fixed ISP color settings only; "
                 "it does not execute the full configured ISP graph.",
                 "Observed RAW metric subtracts black only; nonlinear dark response remains."]
        summary["prediction"]["included_effects"] = effects.copy()
        summary["array_units"].update({
            "hdr_" + key: unit for key, unit in merged["summary"]["array_units"].items()
        })
        return output
