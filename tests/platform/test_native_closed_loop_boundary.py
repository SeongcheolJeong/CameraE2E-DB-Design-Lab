"""Do not advertise or queue four-site feedback before its statistics exist."""

import pytest

from camera_platform.closed_loop import ClosedLoopSceneRuntime, request_template
from camera_platform.runtime import RuntimeRequestError
from camera_platform.temporal import TemporalCaptureSession


def test_native_feedback_refuses_before_capture_and_capabilities_agree(monkeypatch):
    request = request_template()
    # Presence selects the unsupported contract before any heavy optical work.
    request["native_pixel_response"] = {"schema": "camera_native_pixel_response_v1"}
    monkeypatch.setattr(TemporalCaptureSession, "__init__",
                        lambda *args: pytest.fail("Unsupported feedback reached capture"))
    runtime = ClosedLoopSceneRuntime()
    issues = runtime.validate_request(request)
    assert issues and "closed-loop AE/AWB statistics" in str(issues)
    with pytest.raises(RuntimeRequestError, match="closed-loop AE/AWB statistics"):
        runtime.execute(request)
    capability = runtime.describe_capabilities()
    assert "native_four_site_absolute_response" not in capability["supports"]
    assert "native_four_site_feedback" in capability["unsupported"]
