# 저장된 센서 revision의 실행 연결

`camera_platform.sensor_db_bridge`는 기존 `sensor_simulations.sqlite`의 **저장된 run**을 선택해 기존 `pyisetcam` 계산 경로에서 RAW를 얻는다. 재계산 시 DB의 최신 모델을 다시 생성하지 않는다. `run_id`를 생략하면 현재 run을 먼저 선택한 뒤 그 ID로 실행을 고정한다. 결과의 `revision`에는 센서 ID, 보고서 코드, run ID, 결과·설정 SHA-256이 들어간다.

```python
from camera_platform.sensor_db_bridge import (
    sensor_revision_catalog, inspect_sensor_revision, run_sensor_revision,
    compare_sensor_revisions,
)

choices = sensor_revision_catalog(query="DEF-2206-805")
run_id = choices[0]["run_id"]
evidence = inspect_sensor_revision("DEF-2206-805", run_id=run_id)
capture = run_sensor_revision(
    "DEF-2206-805", run_id=run_id,
    scene={"type": "uniform ee", "args": [[8, 8]]},
    sensor_size=(8, 8), exposure_s=0.004,
    readout_overrides={"gain": 2.0}, noise_flag=0, seed=7,
)
raw_dn = capture["raw"]["values"]

other = sensor_revision_catalog(query="DEF-1801-802")[0]
comparison = compare_sensor_revisions(
    [
        {"identifier": "DEF-2206-805", "run_id": run_id},
        {"identifier": other["report_code"], "run_id": other["run_id"]},
    ],
    scene={"type": "uniform ee", "args": [[8, 8]]},
    sensor_size=(8, 8), exposure_s=0.004,
    readout_overrides={
        "gain": 1.0, "full_well_e": 10000.0, "read_noise_e": 10.0,
        "dark_current_e_per_s": 10.0, "bit_depth": 12, "black_level_dn": 64.0,
    },
)
raws = [item["raw"]["values"] for item in comparison["captures"]]
```

`inspect_sensor_revision()`은 `executable`, 저장 모델의 native CFA·파장·pixel pitch, readout 필드별 출처 분류, 광학/수집 가정, FDTD/TCAD 사용 여부를 반환한다. 저장 run이 제외·실패 상태이거나 물리 estimate에 활성 FDTD/TCAD 층이 같이 선언되어 있으면 실행을 거부한다. `run_sensor_revision()`의 `capture`는 **저장 readout 값**, **명시적 override**, **실제로 적용된 readout 값**을 별도로 기록한다. RAW는 int32 DN, 광전하는 electron/pixel 단위로 제공한다. 선택한 모델의 수치 검증은 실측 정확도나 제품 우열을 보증하지 않는다.

현재 실행 입력은 8–32 픽셀의 `uniform ee` 정적 장면, 단일 독립 노출, `noise_flag` 0 또는 2에 한정한다. 동일한 장면 매개변수를 서로 다른 센서에 쓸 수 있지만, 서로 다른 pixel pitch에서 같은 물리 화각을 보장하는 비교는 아니다. 공간 선예도·색·과제 성능 지표나 제품 순위는 이 연결의 출력이 아니다.

`compare_sensor_revisions()`는 정확히 두 저장 revision을 같은 장면·노출·크기·readout 설정으로 실행한다. 두 모델의 저장 readout이 다르면, 다른 필드를 공통 `readout_overrides`에 명시해야 한다. `noise_flag=2`에서는 저장 fixed-pattern profile도 같아야 한다. 결과에는 두 실행의 RAW 원본을 각각 보존하고, 평균 RAW DN·black level을 뺀 DN·평균 광전자 수에 대한 `second_minus_first`, `absolute_difference`, `relative_difference_to_first`를 기록한다. 분모인 첫 값이 0이면 상대 차이는 `null`이다. 모델 종류·pixel pitch·CFA·readout 출처의 차이도 `source_differences`에 남긴다. 이는 집계 수치의 변화이며 픽셀별 정합, 동일 물리 화각, MTF·색 성능 또는 제품 우열 비교가 아니다.

저장 DB의 `physical_estimate_v1`은 R/Gr/Gb/B 네 사이트와 400–700 nm 응답·독립적인 수집 의미를 갖는다. R1 기본 자산의 두 파장/세 QE 채널, PSF sample pitch, 기본 전기 변환 계약에 이를 그대로 복사하면 물리 의미가 바뀐다. `r1_component_compatibility`가 이 직접 변환을 차단하는 이유를 설명한다. 기존 `camerae2e_compare_sensors()`는 유용한 고정 장면 SNR·색·slanted-edge 비교를 제공하지만, 저장 run ID를 사용하지 않고 `db_estimate_v1`을 다시 계산한다. 그 지표를 이 저장 revision의 평가 결과로 표시해서는 안 된다.

현재 저장된 simulation DB의 모델 종류는 `db_estimate_v1`과 `physical_estimate_v1`이다. 기존 FDTD/TCAD LUT는 `image_sensor_db_config(model="legacy")`와 `system_faca`에서 별도로 선택·적용할 수 있다. 이 연결은 둘을 저장된 estimate에 합성하지 않는다. 센서별 LUT의 구조·방향·정규화·전하 수집 계약을 확인한 후 별도의 실행 모드로 추가해야 한다.
