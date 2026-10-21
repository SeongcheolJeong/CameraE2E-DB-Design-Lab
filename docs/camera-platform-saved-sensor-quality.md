# 저장 센서 run 화질 진단

이 작업은 `sensor_simulations.sqlite`에 이미 저장된 **특정 run ID**의 native
응답·CFA·readout을 사용한다. source catalog에서 새 `db_estimate_v1`을 생성하는
[카탈로그 화질 비교](camera-platform-quality-comparison.md)와 다른 계산이다.
제출 시 결과/config 해시와 실행 코드·상류 자산을 고정하고 Worker에서 다시 확인한다.
현재 저장 `db_estimate_v1`·`physical_estimate_v1` run만 이 실행 계약을
충족한다. 저장 `fdtd_estimate_v1`은 기존 시나리오 경로가 LUT 모델을 다시
생성하므로 제출 전에 거부한다.

`GET /api/v1/saved-sensor-quality/template`은 저장 run 목록과 정확한 요청 틀을
반환한다. 1–2개 run, `uniform`·`slanted_edge`·`macbeth_d65`·
`hdr_chart_d65` 중 1–3개 장면,
16–32 픽셀 출력, 장면당 2–4개 프레임으로 제한한다. 동일한 장면 광량과 노출을
선언하지만 픽셀 크기가 다른 센서의 물리 화각을 자동으로 맞추지 않으므로
숫자를 제품 순위로 해석해서는 안 된다.
브라우저에서는 `/saved-sensor-workbench.html`에서 run과 장면을 선택해 제출하고
저장 결과를 다시 열 수 있다.

`hdr_chart_d65`는 기존 `pyisetcam.scene_create("hdr chart", ...)`로 만든
6단계 D65 차트다. 최소 32×32 출력에서 단계별 입력 휘도와 native RAW·
결정론적 DN·평균 광전자·포화 진단을 보존한다. 전체 장면의 평균 휘도를
선언값으로 맞추되 단계 사이의 비율은 유지한다. **한 번의 노출**에 대한
응답 곡선이므로 HDR fusion, 다중 노출, LED flicker/LFM 결과가 아니다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8770") as client:
    template = client.saved_sensor_quality_template(query="DEF-1801-802")
    saved = next(row for row in template["sensor_catalog"]
                 if row["report_code"] == "DEF-1801-802")
    spec = template["specification"]
    spec.update(
        sensor_revisions=[{"identifier": saved["report_code"],
                           "run_id": saved["run_id"]}],
        scenes=["uniform", "slanted_edge", "macbeth_d65"],
        acknowledge_simulation_limits=True,
    )
    job = client.submit_saved_sensor_quality(spec)
    result = client.wait(job["id"], expected_kind="saved_sensor_quality")
    assert result["status"] == "succeeded", result["error"]
    for case in result["result"]["summary"]["sensor_reports"][0]["cases"]:
        print(case["scene"], case["metrics"])
```

저장 run의 각도 응답은 원래 F/4 pupil 조건에 묶여 있으므로 계산의 이상 광학을
그 조건에 고정한다. 각 장면의 첫 정수 RAW와 평균·분산을 저장하고 HDR 차트에서는
반복 촬영한 모든 정수 RAW도 저장한다. 선형 ISP 결과와 지표를
NPZ에, 실행·출처·지원 한계를 JSON에 남긴다. 시간 SNR은 독립 프레임의
분산으로 계산한다. edge MTF50은 **고정 이상 광학을 포함한 시스템 모델**의
진단값이지 센서 고유 MTF가 아니다. Macbeth CIE76은 같은 패치를 이용해
보정·평가한 잔차로, 독립 색 정확도 검증이 아니다. 포화나 지원하지 않는 CFA,
MTF crossing 부재는 이유와 함께 해석 불가로 기록한다.

이 작업에는 Lens DB revision이 들어가지 않는다. 현재 저장 Lens PSF는 절대
광량과 센서 픽셀 샘플링이 검증되지 않아 Lens×Sensor RAW를 생성하지 않는다.
모든 결과의 물리 정확도는 실측으로 검증되지 않았다. 대표 해상도와 장시간
Worker 부하는 별도 수용 시험이 필요하다.
