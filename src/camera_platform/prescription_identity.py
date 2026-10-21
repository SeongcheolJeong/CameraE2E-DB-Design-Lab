"""Versioned identity for separately sampled, otherwise identical Lens requests.

Only the v3-v5 ``sampling`` block is numerical quadrature and convergence
control. Every other declared field remains part of the physical identity,
including wavelengths, field/image coordinates, focus, surfaces, media, stop
transmission, illumination, units and finite object conjugate. The two full
request hashes remain separate provenance; this identity never replaces them.
"""

from __future__ import annotations

from .registry import digest

SCHEMA = "camera_lens_physical_prescription_binding_v1"
SUPPORTED_SCHEMAS = frozenset(
    {"lens_prescription_v3", "lens_prescription_v4", "lens_prescription_v5"}
)


def _physical_sha256(request: dict) -> str:
    from .lens_factory import validate_request

    if not isinstance(request, dict) or request.get("schema") not in SUPPORTED_SCHEMAS:
        raise ValueError(
            "PSF and radiometry parent must preserve the same prescription; "
            "independent numerical sampling requires a v3-v5 Lens prescription"
        )
    normalized = validate_request(request)
    if normalized != request:
        raise ValueError("Require the exact frozen normalized Lens prescription")
    physical = {key: value for key, value in normalized.items() if key != "sampling"}
    if set(normalized) - set(physical) != {"sampling"}:
        raise ValueError("The Lens sampling block must be the only omitted field")
    return digest({"schema": SCHEMA, "physical_prescription": physical})


def sampling_independent_binding(parent_request: dict, psf_request: dict) -> dict | None:
    """Return a binding only when validated v3-v5 requests differ in sampling.

    Identical requests keep their historical exact-match contract and model
    bytes. Earlier request versions retain exact-match semantics altogether.
    """
    if parent_request == psf_request:
        return None
    if (
        not isinstance(parent_request, dict)
        or not isinstance(psf_request, dict)
        or parent_request.get("schema") != psf_request.get("schema")
    ):
        raise ValueError("PSF and radiometry parent must preserve the same prescription")
    parent_physical = _physical_sha256(parent_request)
    psf_physical = _physical_sha256(psf_request)
    if parent_physical != psf_physical:
        raise ValueError("PSF and radiometry parent must preserve the same prescription")
    return {
        "schema": SCHEMA,
        "physical_prescription_sha256": parent_physical,
        "radiometry_request_sha256": digest(parent_request),
        "psf_prescription_request_sha256": digest(psf_request),
        "excluded_numerical_field": "sampling",
    }
