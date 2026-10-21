# 기존 DB revision 비교: 개발 소스 `0.16.2.dev5`

이 문서는 **dev5의 `db-comparisons` 작업 계약**을 보존한다. 이후 개발 소스에는
별도의 [Lens×Sensor 정수 RAW 근사 미리보기와 HDR·LFM·ISP·데이터셋 작업](camera-platform-integrated-workflows.md)이 추가됐다.
아래의 “두 트랙은 서로 결합하지 않는다”는 설명은 `db-comparisons` 작업에만
해당한다. 새 결합 미리보기도 렌즈 광량·회절·픽셀 sampling 검증 전이므로
제품 성능 비교에 사용하지 않는다.

이 경로는 보유 DB에서 자산을 고르고 같은 API·SDK·Worker를 통해 계산과 원본을
저장하기 위한 첫 사용자 흐름이다. **고정된 R1 `0.16.1rc2` ZIP/wheel에는
포함되지 않는다.** 별도 개발 state에서 실행한다.
[기존 계산 엔진 전체와 현재 플랫폼 연결 범위](camera-platform-existing-engine-adoption.md)는
별도 기능 지도로 정리했다.

## 현재 가능한 두 트랙

| 트랙 | 선택 및 계산 | 결과가 뜻하지 않는 것 |
| --- | --- | --- |
| Lens | 부품 카탈로그의 revision ID 두 개를 선택해 해시 검증된 v2 기하 PSF에서 같은 파장·물체 거리의 중심/주변부 EE80, RMS spot, CRA를 계산한다. | 절대 광량, 회절 포함 MTF, 센서 pixel sampling, 실제 렌즈 선정 순위 |
| Sensor | 저장 센서 시뮬레이션의 report/run ID 두 개를 선택해 native CFA·분광 응답·readout으로 같은 선언 uniform 장면과 노출에서 정수 RAW·평균 광전자 수를 계산한다. readout 값이 다르면 공통 override를 명시해야 한다. | 동일 물리 화각, 제품 전기 성능 순위, 실측 정확도, HDR/LFM |

두 트랙은 **한 작업·한 저장 결과에서 함께 보지만 서로의 물리 입력으로 합성하지
않는다.** 렌즈 쪽 검증 PSF는 조건부 합 1 기하 ray histogram이며 절대
transmission·상대 조도·센서 샘플링이 미확립이다. 임의 상수를 붙여 센서 RAW에
합치면 2×2 결과처럼 보이지만 의미가 달라진다. 보고서의
`lens_sensor_cross_product.status`는 `not_executed`다. 기존 DB 전체 revision이
곧 실행 가능하다는 뜻도 아니다. catalog의 `preview_status`와 `reason_code`를
먼저 확인한다.
센서 catalog의 `simulated`는 저장 보고서 상태다. 제출 시에는 선택한 run의
저장 코드 해시와 현재 실행 코드도 대조하고, 맞지 않으면 작업을 큐에 넣기 전에
이유를 반환한다. 현재 저장 `fdtd_estimate_v1` run은 기존 시나리오 실행기가
고정 snapshot을 사용하는 대신 LUT 모델을 다시 생성하므로 이 DB 선택 경로에서
실행 불가로 표시한다. 별도 FDTD LUT 민감도 작업은 아래에 있다.

API 진입점은 `GET /api/v1/db-comparisons/template`,
`POST /api/v1/db-comparisons`, `GET /api/v1/jobs/{id}`,
`GET /api/v1/db-comparisons/{id}`, `GET /api/v1/artifacts/{sha256}`다.
공통 jobs 조회는 상태만 제공하고, 전문 GET은 저장 결과·원본 RAW artifact를
해시 검증한다. 브라우저는 `/db-comparisons.html`에서 같은 경로를 사용한다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8770") as client:
    template = client.db_comparison_template()
    lenses = [row for row in template["lens_catalog"]["revisions"]
              if row["preview_status"] == "ready"]
    sensors = {row["report_code"]: row for row in template["sensor_catalog"]}
    spec = template["specification"]
    spec.update(
        name="DB revision 비교",
        lens_revision_ids=[lenses[0]["revision_id"], lenses[1]["revision_id"]],
        sensor_revisions=[
            {"identifier": code, "run_id": sensors[code]["run_id"]}
            for code in ("DEF-1801-802", "DEP-2209-801")
        ],
        acknowledge_simulation_limits=True,
    )
    job = client.submit_db_comparison(spec)
    result = client.wait(job["id"], expected_kind="db_comparison")
    assert result["status"] == "succeeded", result["error"]
    summary = result["result"]["summary"]
    print(summary["sensor_raw"]["differences"])
    print(summary["lens_geometry"]["lenses"])
    print(result["artifacts"])
```

위 두 센서 report는 현재 작업공간 예시이며 다른 DB에서는 catalog에 없을 수 있다.
선택한 두 저장 readout의 여섯 필드가 다르면 API는 제출 전에 불일치 필드를
알려준다. 공통 `readout_overrides`에 그 필드를 명시하고 다시 제출할 수 있지만,
그 결과는 바꾼 회로 가정의 민감도다. `noise_flag=0`은 결정론적 비교이고,
`noise_flag=2`는 고정 패턴 설정까지 맞아야 한다. 두 센서의 평균 DN과
광전자 수 차이는 두 번째 선택−첫 번째 선택으로 표기한다. 시스템은 순위를
자동으로 부여하지 않는다.

개발 서버 예시는 다음과 같다. 기본 운영 state 또는 RC2 state가 아니라 새
state를 지정한다.

```sh
cd /Users/seongcheoljeong/Documents/CameraE2E
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-platform/bin/python -m camera_platform serve \
  --state-dir /private/tmp/camera-db-comparisons-dev-state --workspace "$PWD" \
  --port 8770 --empty
```

서버 CLI 인자는 `python -m camera_platform --help`로 확인한다. 작업 디렉터리와
DB가 없는 별도 설치 환경에서는 catalog 탐색이 비거나 명확하게 실패한다.
RC2 패키지 설치 검증과 이 개발 서버 실행은 별도의 증거다.

이 다섯 작업은 `--workspace` 아래의
`.cache/upstream/isetcam/412b9f9bdb3262f2552b96f0e769b5ad6cdff821/`에
사전 준비된 ISETCam 자산을 사용한다. `pyisetcam`의 일반 용도는 첫 호출 시
snapshot을 받을 수 있지만, 플랫폼 비교 작업은 **실행 중 다운로드하지
않는다.** 필요한 로컬 자산이 없거나 실행 중 바뀌면 작업은 실패하고,
제출 시 사용한 공통 조명·색 필터·표시·색 공간 파일의 해시를 결과에 남긴다.
wheel과 렌즈·센서 DB는 별도 자산이다. 설치된 wheel을 checkout 밖에서 실행할
때도 `--workspace`에는 해당 DB와 snapshot을 보유한 경로를 지정해야 한다.
현재 렌즈 catalog의 일부 원본 URI는 생성 당시의 절대 경로로 저장돼 있다.
복사한 작업공간에서는 이 역사적 URI를 변경하지 않고, **선택한 DB root 안의
표준 Lens DB 경로**에서 같은 파일명·SHA-256을 가진 원본을 하나만 찾을 때
연결한다. 원본 파일이 없거나 해시가 다르고, 심볼릭 링크 또는 중복 후보가
있으면 실행을 거부한다. 이는 DB 이전 시 자산 정체성을 지키는 연결이며
광학 모델의 광량·샘플링 정확도를 높이는 변환은 아니다.

## 남은 제품 수용 기준

1. 기하 PSF가 아닌 광량·회절·기준면·좌표·센서 sampling 검증을 가진 렌즈
   revision을 실행 가능한 광학 모델로 받아들여야 렌즈×센서 RAW를 만들 수 있다.
2. 저장 센서 run에서 flat RAW 외 edge·색 장면의 모델 진단을 제공한다.
   다음에는 물리 화각을 맞춘 센서 간 비교와 과제 장면을 연결해야 한다.
   출처가 다른 catalog `db_estimate_v1` 품질 비교는 별도 라벨로 둔다.
3. 기존 FDTD/TCAD LUT는 적용 파장·각도·공간 kernel·정규화·수집 모델이
   충족된 경우에만 교체하고, 바뀐 물리량과 원본 RAW를 함께 보존해야 한다.
4. 대표 해상도와 여러 장면의 시간·메모리, 실패·재시도·취소를 확인해야 한다.

이 단계들은 실측 전에도 진행할 수 있지만 계산 일치, 모델 적용 범위,
교차 도구 대조, 실물 정확도 검증을 각각 따로 기록한다.

## 기존 평가기·LUT의 별도 실행 경로

개발 소스는 기존 기능과 저장 run 진단을 별도 작업으로도 노출한다. 이 결과를
선택 렌즈의 결과와 자동 병합하지 않는다.

- [카탈로그 화질 비교](camera-platform-quality-comparison.md):
  `GET/POST /api/v1/catalog-quality-comparisons`에서 source catalog의
  `db_estimate_v1` 센서 2–3개를 동일 D65 조건으로 다시 계산한다. 실제 계산한
  시간 분산 SNR·edge MTF50·Macbeth 잔차 및 RAW/ISP NPZ·HTML을 보존한다.
  저장된 `sensor_simulations.sqlite` run ID와 Lens DB revision은 사용하지
  않으므로 위의 두 revision RAW 결과에 같은 지표가 생긴 것으로 읽지 않는다.
- [저장 센서 run 화질 진단](camera-platform-saved-sensor-quality.md):
  `GET/POST /api/v1/saved-sensor-quality`에서 **pinned run ID**의 native 응답과
  readout으로 flat·edge·Macbeth·D65 HDR 차트 장면을 실행한다. 첫 RAW와
  시간 SNR, 가능한 경우 edge MTF50·색 보정 잔차를 저장한다. HDR 차트에서는
  모든 반복 RAW와 단계별 응답을 저장하지만 **단일 노출**이며 HDR fusion이나
  LFM 평가가 아니다. F/4·4 mm의 고정 이상
  광학, 작은 정적 crop, CFA별 ISP 가능 여부를 함께 확인해야 한다.
- [저장 센서 run 노출·gain 탐색](camera-platform-saved-sensor-search.md):
  `GET/POST /api/v1/saved-sensor-search`에서 기존 `pyisetcam` 최적화기를
  사용한다. 명시한 saved DB/physical run 하나의 응답·readout을 고정하고 최대
  4개 노출·gain 후보의 평균 광전자와 well/ADC 포화 제약을 계산한다. 결과는
  지정 후보와 목적함수의 선택이며 RAW 배열·SNR·HDR·제품 순위가 아니다.
- [FDTD/TCAD LUT 민감도](camera-platform-physics-lut-bridge.md):
  `GET/POST /api/v1/physics-lut`에서 FDTD 중심 대비 field 광흡수 또는
  TCAD 중심 대비 전하 수집 **하나만** 기본 generic 센서에 적용한다.
  적용 전후 두 RAW와 실제 LUT·sidecar 해시, 정규화·정확도 상태를 보존한다.
  저장 센서 revision에 추가 합성하지 않는다.

Agent는 각각 `client.catalog_quality_template()` →
`client.submit_catalog_quality_comparison(spec)` → `client.wait(job["id"])`,
`client.saved_sensor_quality_template()` →
`client.submit_saved_sensor_quality(spec)` → `client.wait(job["id"])`,
`client.saved_sensor_search_template()` →
`client.submit_saved_sensor_search(spec)` → `client.wait(job["id"])`,
`client.physics_lut_template()` →
`client.submit_physics_lut_sensitivity(spec)` → `client.wait(job["id"])` 순서를
사용한다. 각 템플릿의 지원 범위 확인 필드를 명시해야 한다. 다섯 작업의
`wait()`은 공통 lifecycle을 폴링한 뒤 해당 전문 결과를 반환한다.

[개발 wheel 설치·복사 DB·Worker 검증](../reports/camera_platform/db-integration-20260927/development-evidence-dev5.json)은
`0.16.2.dev5`의 근거다. 브라우저 화면 검증 여부는 해당 증거의
`boundaries` 필드를 확인한다. 새 작업은 API·SDK·설치형 Worker로 검증했다.
실측 기반 제품 승인과 상용 설치 적합성은 여전히 별도다.
