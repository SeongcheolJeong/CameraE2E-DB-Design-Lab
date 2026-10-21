# 저장 센서 run의 제한된 노출·gain 탐색

이 경로는 기존 `pyisetcam.camerae2e_optimize_parameters()`를 **저장된 특정
센서 run**에 연결한다. 노출과 analog gain의 선언값만 바꾸고 저장된 native
응답, readout profile, F/4·4 mm 이상 광학, 균일 equal-energy 장면을 사용한다.
탐색 후보는 최대 4개다. 저장 run ID·결과/config 해시, 실행 코드와 상류 자산은
제출·실행 시 확인한다.
현재 저장 `db_estimate_v1`·`physical_estimate_v1`만 지원한다. 저장
`fdtd_estimate_v1`은 고정된 모델 snapshot 실행이 확인되지 않아 거부한다.

`GET /api/v1/saved-sensor-search/template`에서 run 목록과 요청 틀을 받는다.
`POST /api/v1/saved-sensor-search`로 제출한 뒤 공통
`GET /api/v1/jobs/{id}` 또는 SDK `wait()`으로 상태를 확인한다.
브라우저에서는 `/saved-sensor-workbench.html`에서 같은 템플릿을 사용한다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8770") as client:
    template = client.saved_sensor_search_template(query="DEP-2209-801")
    saved = next(row for row in template["sensor_catalog"]
                 if row["report_code"] == "DEP-2209-801")
    spec = template["specification"]
    spec.update(
        sensor_revision={"identifier": saved["report_code"],
                         "run_id": saved["run_id"]},
        exposure_s=[0.001, 0.004],
        analog_gain=[1.0, 2.0],
        max_saturation_fraction=0.0,
        acknowledge_simulation_limits=True,
    )
    job = client.submit_saved_sensor_search(spec)
    result = client.wait(job["id"], expected_kind="saved_sensor_search")
    assert result["status"] == "succeeded", result["error"]
    search = result["result"]["summary"]["search"]
    for case in search["cases"]:
        print(case["parameters"], case["mean_electrons_e_per_pixel"], case["mean_raw_dn"])
```

선택 기준은 **평균 광전자 수 최대화**이며 well·ADC white 포화 비율을
제약으로 둔다. gain은 광전자 수를 늘리지 않으므로 readout 코드값과 포화에
대한 영향으로 구분한다. `best_case`는 이 기준과 지정한 네 후보 안에서만의
선택이다. 시간 SNR·공간 해상력·색·인지 지표의 최적값이나 제품 순위가 아니다.
탐색은 결정론적 `noise_flag=0`로 수행하고 case별 원본 RAW 배열은 내보내지
않는다. 후보로부터 RAW 데이터셋을 생성하려면 별도 실행·seed·출처를 가진
재계산으로 기록해야 하며, 기존 탐색 RAW와 같다고 주장해서는 안 된다.

기존 엔진에는 더 많은 탐색 알고리즘과 변수, 데이터셋 export가 있다. 이
플랫폼 작업은 그중 **저장 run의 응답 계약을 유지하는 제한된 grid**만 연다.
다른 모델·목적함수·Scene을 자동 조합하려면 각각의 적용 조건과 평가 지표를
별도로 확인해야 한다.
