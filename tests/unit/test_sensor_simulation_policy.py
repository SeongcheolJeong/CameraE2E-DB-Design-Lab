"""Regression coverage for real report-scope and native-CFA misclassifications."""

import re

import pytest

from pyisetcam.sensor_simulation_policy import _manifest, sensor_simulation_policy


@pytest.mark.parametrize(
    ("code", "reason_fragment"),
    [
        ("DEF-1809-802", "Polarization"),
        ("DEP-1811-802", "Polarization"),
        ("DEF-2502-802", "UV"),
        ("DEP-2504-801", "UV"),
        ("DEF-2011-803", "4.2"),
        ("DEF-2204-804", "Nonacell"),
        ("DEF-2304-807", "four native pixel"),
        ("DEP-2511-801", "4x4"),
        ("DEP-2004-802", "Quad Quad"),
        ("DEF-2604-801", "Quad-Quad"),
    ],
)
def test_known_nonmatching_sensor_scope_is_excluded(code, reason_fragment):
    policy = sensor_simulation_policy(code)
    assert policy["action"] == "exclude"
    assert reason_fragment in policy["reason"]
    assert policy["evidence"]
    assert "model_override" not in policy


@pytest.mark.parametrize(
    "code", ["DEF-2305-802", "DEF-2403-801", "DEF-2409-801", "DEP-2505-802"]
)
def test_source_confirmed_four_cell_uses_quad_db_model(code):
    policy = sensor_simulation_policy(code)
    assert policy["action"] == "override"
    assert policy["model_override"] == "db_estimate_v1"
    assert policy["cfa_pattern_override"] == "quad_bayer"
    assert policy["angular_response_supported"] is False
    assert policy["measured_accuracy_verified"] is False
    assert any(e.get("field") == "color_filter_pitch_um" for e in policy["evidence"])


@pytest.mark.parametrize("code", ["DEF-2506-802", "DEP-2506-801"])
def test_metaphotonic_lens_falls_back_without_inventing_native_cfa(code):
    policy = sensor_simulation_policy(code)
    assert policy["action"] == "override"
    assert policy["model_override"] == "db_estimate_v1"
    assert "cfa_pattern_override" not in policy
    assert policy["angular_response_supported"] is False


@pytest.mark.parametrize("code", ["DEF-2102-801", "DEF-2409-803"])
def test_ambiguous_pixel_unit_is_not_forced_into_quad_bayer(code):
    policy = sensor_simulation_policy(code)
    assert policy["action"] == "exclude"
    assert "cfa_pattern_override" not in policy


def test_unlisted_code_does_not_gain_measurement_or_model_eligibility():
    policy = sensor_simulation_policy(" def-2206-805 ")
    assert policy["report_code"] == "DEF-2206-805"
    assert policy["action"] == "allow"
    assert policy["source_reviewed"] is False
    assert policy["measured_accuracy_verified"] is False
    assert "mandatory" in policy["reason"]
    assert "model_override" not in policy
    assert sensor_simulation_policy("DEF-2011-801")["action"] == "allow"


def test_explicit_monochromatic_report_recovery_has_narrow_transmission_scope():
    policy = sensor_simulation_policy("DEP-1902-801")
    assert policy["action"] == "override"
    assert policy["model_override"] == "db_estimate_v1"
    assert policy["cfa_pattern_override"] == "monochrome"
    assert policy["source_reviewed"] is True
    recovery = policy["missing_cfa_recovery"]
    assert recovery["transmission_model"] == "unity_color_filter_absorption_only"
    assert "does not imply unit optical-stack throughput or QE" in recovery["transmission_scope"]
    assert recovery["original_db_modified"] is False
    assert any("monochrom" in e["quote"].lower() for e in policy["evidence"])


def test_policy_lookup_returns_deeply_independent_snapshots():
    first = sensor_simulation_policy("DEF-2403-801")
    first["evidence"][0]["quote"] = "changed by caller"
    first["action"] = "allow"
    second = sensor_simulation_policy("DEF-2403-801")
    assert second["action"] == "override"
    assert second["evidence"][0]["quote"] != "changed by caller"


def test_all_scope_corrections_retain_exact_source_identity_and_real_page_semantics():
    manifest = _manifest()
    for code, entry in manifest["entries"].items():
        for evidence in entry["evidence"]:
            assert re.fullmatch(r"[A-Z]{3}-\d{4}-\d{3}", evidence["source_report_code"])
            assert evidence["source_path"]
            assert re.fullmatch(r"[0-9a-f]{64}", evidence["source_sha256"])
            assert evidence["quote"]
            page = evidence["pdf_page_1_based"]
            assert page is None or (isinstance(page, int) and page >= 1)
            if evidence["source_kind"] == "cached_pdf_text":
                assert page is not None, code
            if evidence["source_kind"] == "local_report_html":
                assert page is None, code


@pytest.mark.parametrize("value", [None, 123, True, "IMX662", "DEF-22-805", ""])
def test_device_alias_and_invalid_report_codes_cannot_select_a_policy(value):
    with pytest.raises(ValueError, match="report_code"):
        sensor_simulation_policy(value)
