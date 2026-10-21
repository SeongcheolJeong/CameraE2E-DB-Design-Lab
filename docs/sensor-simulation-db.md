# 저장된 이미지센서 시뮬레이션 DB

이 DB는 CameraE2E에서 실행한 센서별 시뮬레이션의 구성, 결과 파일, 입력 근거와
해시를 보관한다. 센서 사양 목록과 실제 계산 결과를 구분하며, 실행하지 못한 센서도
제외·실패 사유를 남긴다. 저장됐다는 사실이나 수치 검사를 통과했다는 사실이
센서 실측 정확도를 검증하지는 않는다.

이번 실행의 센서별 결과는 [검색 가능한 결과표](../reports/sensor_simulation_db/index.html),
[CSV 목록](../reports/sensor_simulation_db/inventory.csv),
[실행·검증 보고서](../reports/sensor_simulation_db/implementation.md)에서 확인한다.

기본 위치는 `camerae2e_db/image_sensor/simulations/sensor_simulations.sqlite`이다.
시뮬레이션 DB API의 `root`는 **CameraE2E DB의 루트**이며, 원본 센서 카탈로그의
`fdtd_tcad/sensor_db` 디렉터리와 다르다. 생략하면 `PYISETCAM_CAMERA_DB_ROOT`
환경변수를 사용하고, 환경변수가 없으면 프로젝트의 `camerae2e_db`를 사용한다.
`root`를 생략했을 때 `PYISETCAM_SENSOR_SIMULATION_DB_PATH`가 지정되어 있으면
해당 SQLite 경로가 우선한다. DB를 이동할 때는 SQLite 옆의 `artifacts`와 `inputs`
디렉터리를 함께 이동한다. 결과 파일의 경로는 이 DB 디렉터리를 기준으로 해석한다.
기존 원본 PDF와 센서 입력 DB는 별도로 유지한다.

## 생성과 재개

```bash
# 현재 센서 카탈로그 전체를 계산하고 기본 CameraE2E DB에 저장한다.
python tools/build_sensor_simulation_db.py --size 32 --frames 8

# 별도 출력 DB에 한 센서의 작은 확인 실행을 저장한다.
python tools/build_sensor_simulation_db.py \
  --root /tmp/camerae2e_sensor_smoke \
  --sensors DEF-2206-805 --size 8 --frames 2 \
  --report /tmp/camerae2e_sensor_smoke/build.json
```

`--catalog-root`는 입력 센서 카탈로그 디렉터리를 지정하고, `--root`는 출력
CameraE2E DB 루트를 지정한다. 기본 계산 조건은 D65/A/F11 조명, 1/4/16 ms 노출,
32×32 RAW, 조건별 8프레임이다. 지원하는 모델은 파장·각도·동공 응답을 함께
계산한다. 미지원 응답을 정상 입사 값으로 채워 넣지 않으며, 정확한 조건·가정과
제한은 각 결과의 metadata에 저장한다.

동일 입력·코드·계산 조건의 실행은 저장 결과의 무결성을 확인한 뒤 재사용한다.
입력 해시가 바뀌거나 `--force`를 지정하면 새 실행을 추가하고 이전 결과를 보존한다.
센서별로 저장하므로 중단 후 같은 명령으로 재개할 수 있다. 이 명령은 실제 계산과
쓰기 작업이며 아래 조회 API와 구분된다.

입력 식별에는 패키지 Python 코드, 광학 자료, 조명·시각·센서 등 사용 가능한
기준 MAT 자료와 Python/수치 라이브러리 버전이 포함된다. 실행별 전체 입력 해시
목록은 `inputs/<fingerprint>.json`에 보존하며 다음 실행으로 덮어쓰지 않는다.

Python에서는 `build_sensor_simulation_db(root=..., catalog_root=...,
sensor_ids=[...], settings={...}, force=False)`로 같은 작업을 실행한다.

## 조회

```python
from pyisetcam.sensor_simulation_db import (
    sensor_simulation_db_config,
    sensor_simulation_db_get,
    sensor_simulation_db_path,
    sensor_simulation_db_search,
    sensor_simulation_db_summary,
)

print(sensor_simulation_db_path())
summary = sensor_simulation_db_summary()
print(summary["status"], summary["counts"])

matches = sensor_simulation_db_search("IMX662", status="simulated")
for item in matches:
    print(item["sensor_id"], item["model"], item["result_sha256"])

if matches:
    saved = sensor_simulation_db_get(matches[0]["sensor_id"])
    print(saved["metadata"]["model_snapshot"]["kind"])
    print(saved["array_manifest"])  # 배열 이름, 크기, 자료형
    saved_with_arrays = sensor_simulation_db_get(
        matches[0]["sensor_id"], load_arrays=True
    )
    print(saved_with_arrays["arrays"]["wave_nm"])

    # 현재 원본 입력 DB를 다시 해석하지 않고 저장된 모델을 선택한다.
    config = sensor_simulation_db_config(matches[0]["sensor_id"])
    print(config["scenario"]["sensor_model"]["simulation_db"])
```

`summary`와 `search`는 저장된 요약만 읽는다. `get`도 기본값에서는 수치 배열을
읽지 않으며, `load_arrays=True`로 요청할 때 결과 배열을 읽는다. 이 조회 API와
아래 통합 카탈로그 조회는 모델을 다시 만들거나 시뮬레이션을 실행하지 않는다.
DB가 없으면 요약의 상태는 `missing`이며, 단순 조회로 새 DB를 만들지 않는다.
`get`은 기본값에서도 JSON과 NPZ 파일의 해시를 검사하므로 파일 바이트를 읽는다.
배열을 메모리에 해석해 적재하는 동작만 `load_arrays=True`에서 수행한다.

센서 검색 결과에는 `sensor_id`, `report_code`, `device_name`, `manufacturer`,
`model`, `status`, `reason`, `run_id`, `result_path`, `config_sha256`,
`result_sha256`와 provenance가 포함된다. 입력과 결과의 해시는 같은 이름으로
저장된 서로 다른 실행을 구분하는 데 사용한다. 현재 파일과의 일치 검사를 별도로
실행하지 않은 상태에서 해시의 존재 자체를 새 검증 결과로 해석하지 않는다.
`sensor_simulation_db_validate(root=...)`는 저장된 전체 결과의 해시·식별자와
수치 배열을 검사한다. 이는 저장 결과의 무결성과 수치 검사이며 제품 실측 비교가 아니다.

## 저장된 모델로 CameraE2E 실행

```python
from pyisetcam import camera_get, camerae2e_run_scenario
from pyisetcam.sensor_simulation_db import sensor_simulation_db_config

config = sensor_simulation_db_config("DEF-2206-805")
result = camerae2e_run_scenario(
    {
        **config["scenario"],
        "scene": {"type": "uniform ee", "args": [8]},
        "sensor": {"size": [8, 8], "integration_time": 0.004, "noise_flag": 2},
        "sensor_resize": False,
    },
    seed=7,
)
sensor = camera_get(result["camera"], "sensor")
print(sensor.data["raw_dn"].shape)
```

이 예제는 저장된 모델로 **새 장면을 계산**한다. 이전 시험 장면의 RAW를 가져오는
`get(load_arrays=True)`와 용도가 다르다. 모델 입력과 기본 readout 프로파일은 저장된
실행을 기준으로 고정하며, 장면·노출 등 새 시나리오 설정은 별도로 지정한다.
이전 실행을 고정하려면 `get`과 `config`에 저장된 `run_id`를 전달한다.
저장 당시와 현재 센서 계산 구현이 다르면 새 장면 실행은 재구성을 요구한다.
과거 결과 배열은 계속 조회할 수 있다. 저장된 입력이 고정됐다는 사실을 과거
실행 코드까지 함께 보존했다는 뜻으로 사용하지 않는다.
특히 CFA 교정이 적용된 센서는 `metadata.config`의 원본 선택 descriptor를 직접
실행하지 말고 `sensor_simulation_db_config`로 저장된 모델을 선택해야 한다.

## 통합 CameraE2E 카탈로그

```python
from pyisetcam import camerae2e_db_get, camerae2e_db_lineage, camerae2e_db_search

database = camerae2e_db_get("sensor_simulation_db")
print(database["metadata"]["summary"]["counts"])

items = camerae2e_db_search("IMX662", tags=["simulation"], include_missing=False)
for item in items:
    print(item["name"], item["readiness_tier"], item["parameters"])
    print(camerae2e_db_lineage(item["name"])["edges"])
```

DB 항목의 이름은 `sensor_simulation_db`, 개별 결과 항목의 이름은
`sensor_simulation:<report_code>`이다. 센서 이름·제조사·ID·보고서 코드·모델로
검색할 수 있다. 각 결과의 dependency는 저장된 시뮬레이션 DB를 가리키며,
입력 근거와 구성 해시는 결과 metadata/provenance에 보존한다.

| 시뮬레이션 DB 상태 | 통합 카탈로그 상태 | 의미 |
| --- | --- | --- |
| `simulated` | `available`, readiness `proxy` | 저장된 추정 결과를 사용할 수 있음 |
| `excluded` | `missing` | 해당 계산에서 제외됨. `metadata.reason` 확인 |
| `failed` | `missing` | 계산에 실패함. `metadata.reason` 확인 |
| `quarantined` | `missing` | 사용 대상에서 격리됨. `metadata.reason` 확인 |

전체 상태와 사유를 확인하려면 기본값인 `include_missing=True`로 조회한다.
`include_missing=False`는 사용 가능한 결과만 남긴다. 여기서 `missing`은 결과의
사용 가능성을 나타내며, 해당 센서의 원본 자료가 없다는 뜻으로 사용하지 않는다.
통합 카탈로그는 저장 결과를 `validated` 측정으로 승격하지 않으며, 엄격한 검증은
이 항목들의 `proxy` 상태를 그대로 보고한다.

## 해석 범위

모델 종류와 가정은 각 실행에 저장된 구성을 기준으로 해석한다.
`physical_estimate_v1`은 DB의 구조값과 명시한 광학·전기 proxy를 결합한 추정이다.
원문을 별도로 검토한 구조값은 해당 센서의 provenance에 명시된다. 모든 센서의
전체 구조를 원문에서 다시 검증한 것은 아니며, 생성된 Si 두께 등도 구분해 보존한다.
CFA `n,k`, 전하 수집효율, 알려지지 않은 readout 항목은 제품별 실측값이 아니다.
DEP-1902-801 GMAX0505의 누락 CFA는 원문의 명시적 흑백 센서 근거로 회수했다.
이 항목의 색필터 흡수에 의한 투과율 1은 전체 광학계 투과율이나 QE가 1이라는 뜻이
아니며, 생성된 Si 두께 5 µm와 총괄 효율 0.75는 계속 가정값으로 기록한다.
명목 QE는 미검증 마이크로렌즈 own-site 손실을 곱하지 않으며, 그 진단으로 주변부
색 음영이나 공간 크로스토크, MTF가 검증됐다고 판단할 수 없다.

관련 사용 범위는 [물리 근사 센서 모델](physical-sensor-e2e.md),
[TechInsights 구조 근거](sensor-techinsights-evidence.md),
[readout 프로파일의 출처](readout-profile-sources.md)에 정리되어 있다.
