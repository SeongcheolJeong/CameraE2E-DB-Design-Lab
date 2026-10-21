"""Explicit applicability of the implemented linear optical response models."""

from __future__ import annotations


def fixed_factory_conditions(model):
    """Keep legacy scopes exact; permit only declared readout-independent optics.

    This does not widen valid_domain. The Composer always checks parent-derived
    numeric bounds separately, including sensor and ISP acquisition bounds.
    """
    conditions = model.get("factory_application_conditions")
    if conditions is None:
        return None
    if model.get("factory_condition_scope", "all_declared") == "optical_response":
        if model.get("kind") != "lens" or not model.get("psf_factory_job_id"):
            raise ValueError("Optical applicability requires a linked PSF Lens model")
        return {k: v for k, v in conditions.items() if k not in {"exposure_s", "gain"}}
    return conditions
