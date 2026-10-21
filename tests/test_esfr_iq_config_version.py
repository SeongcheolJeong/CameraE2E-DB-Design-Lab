"""Versioned IQ settings keep legacy requests and source provenance intact."""
import pytest

from camera_platform import esfr_iq_service as iq
from test_esfr_iq_service import context


SCHEMA = "camera_esfr_iq_config_v1"


def test_iq_template_declares_current_config_version():
    assert iq.iq_template()["schema"] == SCHEMA


@pytest.mark.parametrize("version", ["camera_esfr_iq_config_v2", None, 1])
@pytest.mark.parametrize("enabled", [True, False])
def test_unknown_explicit_version_rejected_before_capture_access(monkeypatch, version, enabled):
    def unexpected(*args, **kwargs):
        pytest.fail("Unknown configuration version must fail before capture or ROI access")
    monkeypatch.setattr(iq, "_captures", unexpected)
    monkeypatch.setattr(iq, "_patches", unexpected)
    with pytest.raises(ValueError, match="configuration schema"):
        iq.normalize_iq(None, {"schema": version, "enabled": enabled}, [], [])


def test_omitted_version_is_stored_and_explicit_current_version_is_equivalent(tmp_path, monkeypatch):
    # The shared service fixture uses real synthetic RAW pixels, and intentionally
    # submits the legacy request without a schema. It never claims physical data.
    service, record, state, jobs, dev, val, payload = context(tmp_path, monkeypatch)
    assert record["iq"]["schema"] == SCHEMA
    assert service.registry.get(iq.WORKFLOWS, record["id"])["iq"]["schema"] == SCHEMA
    explicit = {"schema": SCHEMA, "enabled": True, "reference_id": iq.REFERENCE_ID,
        "actual_isp_color_space": "unknown", "patch_roi_reviewed_capture_ids": [c["id"] for c in dev],
        "noise_repeat_ids": []}
    assert iq.normalize_iq(service, explicit, dev, record["rois"]) == record["iq"]
    assert state["iq_frozen"]["source_kind"] == "synthetic"


@pytest.mark.parametrize("config", [None, {"enabled": False}, {"schema": SCHEMA, "enabled": False}])
def test_disabled_legacy_shape_remains_unchanged(config):
    assert iq.normalize_iq(None, config, [], []) == {"enabled": False}
