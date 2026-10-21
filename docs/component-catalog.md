# 렌즈·이미지 센서 통합 카탈로그

`camerae2e_db/catalog.sqlite`는 렌즈와 센서의 정체성, 사양 근거, 모델 revision,
실행 이력, 결과 파일을 연결하는 부품 카탈로그다. 기존 렌즈 DB와 센서 DB를
읽어서 추가로 생성하며 원본을 수정하지 않는다. 기존 `lens_patent_*`,
`image_sensor_db_*`, `sensor_simulation_db_*` API와 런타임의 모델 선택도 유지한다.
DB/LUT 패키지 경로를 찾는 기존 [DB Catalog](db-catalog.md)와 함께 사용할 수 있다.

## 구축과 재실행

저장소 루트에서 실행한다.

```bash
python tools/build_component_catalog.py --verify-files
```

기본 원본 탐색은 현재 CameraE2E DB의 센서 카탈로그와 렌즈 v9 패키지를 우선한다.
다른 위치에 별도 카탈로그를 만들 때는 원본 경로를 명시한다. 사용자 지정 `--root`
내에 원본이 없다고 해서 관계없는 기본 DB를 가져오지는 않는다.

```bash
python tools/build_component_catalog.py \
  --root /path/to/camerae2e_db \
  --lens-db /path/to/lens_database.sqlite \
  --sensor-catalog /path/to/sensor_db/sensor_catalog.json \
  --sensor-simulations /path/to/sensor_simulations.sqlite \
  --verify-files \
  --report reports/component_catalog/build.json
```

보고서에는 구축 요약과 검증 결과가 들어간다. 구축 오류나 검증 실패는 종료 코드
`2`로 전달한다. 재수입은 같은 정체성과 같은 내용의 행을 중복 생성하지 않으며,
내용이 달라진 모델에는 새 revision을 추가한다. 과거의 실패·제외 실행도 이력으로
보존하므로 과거 실행의 실패와 이번 수입 작업의 실패를 구분해야 한다.
수입 도중 오류가 발생하면 DB 행은 transaction으로 되돌린다. 그 전에 복사한
불변 모델·artifact 파일은 참조되지 않은 채 남을 수 있으므로 전체 디렉터리까지
되돌리는 작업은 아니다. 재시도에서는 동일한 파일 내용을 재사용한다.

## 데이터 구조와 정리 원칙

| 구조 | 역할 |
|---|---|
| `components`, `aliases` | 출처의 정체성으로 만든 고정 ID와 기존 ID·별칭 연결 |
| `sources`, `spec_observations` | 출처 문서·파일, 사양값, 단위, 조건, 원문 위치와 추출 방법 |
| `model_revisions` | 특정 시점의 모델 내용과 가정을 고정한 revision |
| `operating_points` | 초점·조리개, 센서 동작 mode 등의 조건 |
| `simulation_runs` | revision과 조건에 연결된 실행 상태·설정·solver 정보 |
| `artifacts`, `validations` | 결과 파일의 경로·해시·메타데이터와 검증 범위 |
| `lens_surfaces`, `lens_configurations` | 렌즈 면과 configuration별 검색용 사양 |
| `sensor_geometry`, `sensor_layers`, `sensor_readout_modes` | 센서 기하 구조·광학 층·동작 mode |
| `camera_configurations` | 렌즈와 센서의 revision을 고정한 카메라 조합 |

고정 부품 ID는 파일명이 아니라 이름 공간을 포함한 원본 정체성에서 만든다.
기존 ID는 별칭으로 남긴다. 동일한 모델명이 등장해도 다른 센서 보고서를 자동으로
합치지 않으며, 특허 실시예를 양산 렌즈와 같은 제품이라고 간주하지 않는다.

사양 관측값은 출처와 함께 보존한다. 보고된 값, 가정한 값, 계산한 값을 구분하고,
상충하는 관측값을 하나의 값으로 덮어쓰지 않는다. 알 수 없는 수치·조건·비구면
계수 정의는 추측하지 않는다. 원본에 배열의 축·단위·정규화 정보가 있으면 함께
보존하며, 없는 정보는 추가 측정이나 원문 확인이 필요한 상태로 남긴다.

모델 revision과 실행 ID는 별개다. 동일 모델을 다른 solver나 설정으로 계산한
실행을 모델 자체와 혼동하지 않는다. 기존 렌즈 결과에서 원래의 실행 시각 등을
복원할 수 없는 경우에는 저장된 결과와 설정의 snapshot으로 식별한다.

등록하는 파일이 DB 루트 안에 있으면 상대 경로로 연결하고, 밖에 있으면 내용
해시에 따른 `artifacts/` 경로에 복사한다. 없는 파일도 누락 상태로 등록해 원본의
불완전함을 감추지 않는다. 원본 처방이나 센서 구조 snapshot은 revision에 보존된다.
수입에 사용한 원본과 변환 코드의 해시는 `sources/<batch_id>.json`에 저장한다.
센서 재료 경로는 선택한 패키지를 우선하며, 해당 파일이 없으면 원래 카탈로그가
명시한 생성 설정 파일을 기준으로 같은 상대 경로를 확인한다. 이 경우 실제로 읽은
원본 경로와 해시를 기록하고 파일을 `artifacts/`에 보관한다.

## 검색과 상세 조회

```python
from pyisetcam.component_catalog import (
    component_catalog_search,
    component_catalog_get,
    component_catalog_summary,
    component_catalog_validate,
)

root = "/path/to/camerae2e_db"
print(component_catalog_summary(root=root))
lenses = component_catalog_search(family="lens", root=root)
sensors = component_catalog_search("sony", family="sensor", root=root)

if lenses:
    detail = component_catalog_get(lenses[0]["component_id"], root=root)
    print(detail["component"])
    print(detail["revisions"])
    print(detail["observations"])
    print(detail["runs"])
    print(detail["artifacts"])

print(component_catalog_validate(root=root, verify_files=True))
```

상세 조회는 고정 ID 또는 기존 별칭을 받는다. revision은 검토 후 명시적으로
선택한다. 최근 실행이라는 이유만으로 사용 가능한 대표 모델로 채택하지 않는다.

```python
from pyisetcam.component_catalog import component_catalog_select_revision

component_catalog_select_revision(
    "부품 고정 ID 또는 기존 별칭", "검토한 revision ID", root=root
)
```

검증은 카탈로그 관계와 파일 무결성 확인이다. **기존 `validated` 표시를 가져오거나
파일 해시가 일치하는 것만으로 광학·전기적 정확도가 새로 검증되지는 않는다.**
수렴성, 실측 일치, 절대 QE 및 radiometry 보정에는 별도 근거가 필요하다.

## 카메라 조합 고정

검토한 렌즈와 센서의 revision ID를 지정해 조합을 저장한다. 필요한 경우 각 부품의
operating point도 함께 지정한다. `settings`에는 cover glass, IR-cut, 배치, ISP 등
해당 조합을 재현하는 데 필요한 조건을 넣는다.

```python
from pyisetcam.component_catalog import camera_configuration_save

configuration = camera_configuration_save(
    "camera-demo-v1",
    lens_revision_id="검토한 렌즈 revision ID",
    sensor_revision_id="검토한 센서 revision ID",
    root=root,
    settings={"purpose": "조합 검토", "cover_glass": None, "ir_cut": None},
)
print(configuration)
```

이 저장 작업은 CameraE2E 시뮬레이션을 실행하거나 카메라 품질을 보증하지 않는다.
센서 크기와 image circle, CRA와 센서 각도 응답 등의 근거가 충분하지 않으면
호환성은 `unknown`이다. 조합이 저장됐다는 이유로 광학적 호환성을 `passed`로
바꾸지 않는다. 저장된 조합은 `camera_configuration_get(id, root=root)`으로 조회한다.

## Export와 release

회사별 배포 자료는 통합 카탈로그에서 생성한다. Export는 별도의 원본으로 편집하지
않고 통합 카탈로그를 변경한 뒤 다시 만든다. 현재 export는 CSV와 JSON snapshot이며,
원본 바이너리 DB와 모든 결과 배열을 복제한 독립 실행 패키지는 아니다.

```bash
python tools/export_component_catalog.py \
  --root /path/to/camerae2e_db \
  --manufacturer Sony \
  --output-dir /path/to/exports/sony
```

`--manufacturer`를 생략하면 전체 부품을 내보낸다. `components.csv`에는 검색용 부품
목록, `components.json`에는 revision·관측값·실행·artifact 참조와 근거를 저장한다.
실제 생성한 파일 경로는 명령의 JSON 결과에 반환한다.

검토한 revision 집합은 release manifest로 고정할 수 있다.

```python
from pyisetcam.component_catalog import component_catalog_release

release = component_catalog_release(
    "camera-demo-v1",
    root=root,
    revision_ids=["검토한 렌즈 revision ID", "검토한 센서 revision ID"],
)
print(release)
```

새 모델 수입 후에도 기존 release의 선택을 자동으로 바꾸지 않는다. release는
선택과 이력의 고정이며, 공개 배포나 정확도 인증을 뜻하지 않는다. `revision_ids`를
생략할 때는 `component_catalog_select_revision`으로 명시적으로 선택한 대표 revision을
사용한다. 선택도 명시적인 ID 목록도 없으면 오류를 반환하며 최신 revision을 자동
선택하지 않는다.
