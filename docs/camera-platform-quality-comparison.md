# 카탈로그 센서 화질 비교

`camera_platform.quality_comparison_bridge`는 기존
`pyisetcam.sensor_comparison.camerae2e_compare_sensors()`를 제한된 플랫폼 작업으로
연결한다. 카탈로그에서 **두세 개 센서**를 선택하고 같은 D65 장면, 노출,
f-number·초점거리 및 중심 물리 crop으로 각각 계산한다. 이 경로의 센서 응답은
카탈로그 자료로 새로 만든 `db_estimate_v1`이다. 저장된
`sensor_simulations.sqlite`의 특정 run ID를 실행하지 않으며, 특정 Lens DB
revision의 광학 모델을 적용하지도 않는다.

```python
from camera_platform.quality_comparison_bridge import compare_catalog_sensor_quality

result = compare_catalog_sensor_quality(
    ["DEF-1807-801", "DEF-2011-804"],
    scenes=("uniform", "slanted_edge", "macbeth_d65"),
    luminance_cd_m2=10,
    exposure_s=0.004,
    frames=4,
    output_size=(32, 32),
    output_dir="reports/catalog_quality_example",
)
for case in result["cases"]:
    print(case["report_code"], case["scene"], case["metrics"])
```

출력 디렉터리는 새 경로이거나 비어 있어야 한다. 생성된 `comparison.json`은
전체 모델 출처·가정·case·지표를, `comparison_arrays.npz`는 대표 정수 RAW DN,
RAW·ISP 시간 평균/분산 및 원본 광전자 맵을, `index.html`은 비교 화면을 보존한다.
직접 호출의 반환값에는 파일 경로와 SHA-256이 있다. 플랫폼 Worker 작업으로
실행하면 이 세 결과와 요약 결과를 Registry blob으로 옮기고,
`/api/v1/artifacts/{sha256}`에서 다운로드할 수 있다. 완료 작업 조회는 blob의
해시와 요약 결과를 재검증한다.

플랫폼 API 진입점은 `GET /api/v1/catalog-quality-comparisons/template`으로
선택 가능한 카탈로그 모델과 요청 형식을 받고,
`POST /api/v1/catalog-quality-comparisons`로 제출한다. 반환된 작업 ID는
`GET /api/v1/catalog-quality-comparisons/{id}` 또는 공통
`GET /api/v1/jobs/{id}`로 조회한다. 이름·센서 ID·장면·제어값을 명시하고
`acknowledge_simulation_limits=true`를 설정해야 한다. 제출 시 선택한 각
카탈로그 record와 유효 모델 값의 SHA-256 및 실행 환경을 고정하며, Worker는
계산 직전에 같은 값인지 확인한다.

결과 지표의 의미는 다음과 같다.

| 장면 | 출력 | 판독 경계 |
| --- | --- | --- |
| 모든 장면 | 원본 RAW와 공통 출력의 시간 분산 SNR, 광전자 평균, 포화 비율 | 공간 대비를 노이즈로 취급하지 않는다. clipping된 SNR은 진단값만 표시한다. |
| `slanted_edge` | 기존 ISO 12233 처리의 출력 MTF50 | 시간 평균 선형 RGB의 중심 ROI, cycles/output pixel. 계산 불가 시 이유를 표시한다. |
| `macbeth_d65` | 24패치 평균 CIE76 | 같은 D65/Macbeth 표면으로 색 행렬을 맞추고 평가한 **in-sample 잔차**이다. 독립 색 정확도가 아니다. |

이 어댑터는 한 요청에 센서 2–3개, 장면 1–3개, 시간 프레임 2–8개,
출력 16–64 픽셀/축, 하나의 광량·노출 조합으로 제한한다. 물리적인 FWC,
read noise, dark current와 수집 효율은 비교 센서에 공통으로 놓은 가정이며
기본 실행은 sensitivity sweep을 수행하지 않는다. 더 넓은 가정 변화는 기존
[`db-sensor-e2e.md`](db-sensor-e2e.md)의 전체 비교기를 사용한다. 공간 지표
결과를 저장된 센서 simulation revision의 지표로 바꾸어 표기하거나 제품 실측
우열로 해석해서는 안 된다. 저장 revision의 native RAW 비교는
[`camera-platform-sensor-db-bridge.md`](camera-platform-sensor-db-bridge.md)의
별도 경로다.
