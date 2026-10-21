"""Small-process fault injection for large-Scene admission boundaries."""

from __future__ import annotations

import sys

import pytest

from camera_platform.lens_sensor_large_scene_preflight import require_large_scene_resources
from camera_platform.lens_sensor_process_guard import (
    ProcessGuardUnavailableError,
    probe_hard_address_space_limit,
    run_observed_child,
)


def test_hard_limit_probe_never_qualifies_large_scene_from_address_space_only():
    result = probe_hard_address_space_limit()
    assert result["status"] in {
        "limit_set_failed", "allocation_blocked_by_rlimit_as",
        "allocation_succeeded", "probe_failed", "probe_no_valid_child_result",
    }
    assert result["suitable_for_large_scene_admission"] is False
    assert result["hard_rss_limit_verified"] is False


def test_polled_observer_kills_small_memory_fault_and_does_not_promote_state(tmp_path):
    pytest.importorskip("psutil")
    script = (
        "import pathlib,time\n"
        "blocks=[]\n"
        "for i in range(64):\n"
        " blocks.append(bytearray(2*1024**2))\n"
        " time.sleep(0.01)\n"
        "pathlib.Path('should-not-exist').write_text('completed')\n"
    )
    result = run_observed_child(
        [sys.executable, "-B", "-c", script], state_root=tmp_path,
        observed_rss_limit_bytes=32 * 1024**2, timeout_s=3,
        poll_interval_s=0.01,
    )
    assert result["status"] == "observed_rss_limit_exceeded"
    assert result["peak_polled_direct_child_rss_bytes"] >= 32 * 1024**2
    assert result["hard_rss_limit_enforced"] is False
    assert result["descendant_rss_monitored"] is False
    assert result["suitable_for_large_scene_admission"] is False
    assert result["result_promoted"] is False
    assert list(tmp_path.iterdir()) == []


def test_polled_observer_timeout_and_clean_success_are_still_unqualified(tmp_path):
    pytest.importorskip("psutil")
    timeout = run_observed_child(
        [sys.executable, "-B", "-c", "import time;time.sleep(5)"],
        state_root=tmp_path, observed_rss_limit_bytes=128 * 1024**2,
        timeout_s=0.05, poll_interval_s=0.01,
    )
    assert timeout["status"] == "timeout"
    assert timeout["child_exit_code"] != 0
    assert list(tmp_path.iterdir()) == []
    success = run_observed_child(
        [sys.executable, "-B", "-c", (
            "import pathlib,time;pathlib.Path('x').write_text('y');time.sleep(0.05)"
        )],
        state_root=tmp_path, observed_rss_limit_bytes=128 * 1024**2,
        timeout_s=1, poll_interval_s=0.01,
    )
    assert success["status"] == "child_exited"
    assert success["child_exit_code"] == 0
    assert success["result_promoted"] is False
    assert success["suitable_for_large_scene_admission"] is False
    assert list(tmp_path.iterdir()) == []


def test_observer_dependency_refuses_before_launch(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", None)
    with pytest.raises(ProcessGuardUnavailableError, match="child not launched"):
        run_observed_child(
            [sys.executable, "-c", "open('x','w').write('unexpected')"],
            state_root=tmp_path, observed_rss_limit_bytes=1024**2,
            timeout_s=1,
        )
    assert list(tmp_path.iterdir()) == []


def test_forged_within_budget_status_cannot_admit_unimplemented_large_scene_executor():
    with pytest.raises(ValueError, match="resource_blocked_large_scene_executor_unavailable"):
        require_large_scene_resources({
            "status": "within_guarded_process_budget",
            "enforced_child_process_rss_guard": True,
            "hard_memory_boundary_verified": True,
        })
