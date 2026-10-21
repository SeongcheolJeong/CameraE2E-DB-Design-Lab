"""Standalone HTML must expose acceptance scope outside collapsed JSON."""
import pytest

from camera_platform.esfr_report import publish


@pytest.mark.parametrize("status", ["pass", "fail", "inconclusive"])
def test_raw_only_verdict_and_isp_diagnostic_are_visible(tmp_path, status):
    # Authored rendering fixture only: no measured pixels, run or profile is published.
    summary = {"source_kind": "measured", "validation": {
        "status": status, "scope": "raw_native_sfr_only",
        "thresholds": {"raw_sfr_rmse": .03, "isp_sfr_rmse": .05},
        "checks": [{"metric": "isp_sfr_rmse", "value": .2, "threshold": .05, "status": "diagnostic"}]},
        "metrics": {"raw_sfr_rmse": {"before": .2, "after": .01},
                    "isp_sfr_rmse": {"before": .1, "after": .2}}}
    publish(summary, tmp_path)
    text = (tmp_path / "report.html").read_text()
    start = text.index("독립 검증 촬영의 보정 전후 오차")
    visible = text[start:text.index("<details", start)]
    assert 'data-acceptance-scope="raw_native_sfr_only"' in visible
    assert "RAW Gr 단독 기준 판정: <b>" + status in visible
    assert "선형 ISP Y SFR · 진단 전용" in visible
    assert "ISP는 진단 전용이며 합격 판정에 포함되지 않습니다" in visible
    assert "RAW 기준 충족을 ISP 기준 충족 또는 카메라 인증으로 해석하지 않습니다" in visible
    assert "기준값 0.05" in visible
    assert "ISP 전 → 후 · 진단 전용" in text


def test_matched_reference_isp_verdict_keeps_its_joint_scope(tmp_path):
    publish({"source_kind": "synthetic", "validation": {
        "status": "pass", "scope": "raw_and_matched_reference_isp_sfr"}}, tmp_path)
    text = (tmp_path / "report.html").read_text()
    assert 'data-acceptance-scope="raw_and_matched_reference_isp_sfr"' in text
    assert "RAW Gr 단독 기준 판정" not in text
    assert "ISP는 진단 전용이며 합격 판정에 포함되지 않습니다" not in text
