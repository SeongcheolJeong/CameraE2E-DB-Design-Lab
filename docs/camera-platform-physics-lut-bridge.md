# 기존 FDTD·TCAD LUT의 RAW 민감도 연결

`camera_platform.physics_lut_bridge`는 저장된 FDTD 광학 응답이나 TCAD 수집 응답을 기존 `pyisetcam` 카메라 계산에 연결하고, **동일한 기본 센서·장면·노출·readout·seed**에서 원본 RAW와 LUT 적용 RAW를 각각 계산한다. 결과에는 두 int32 DN 배열, 광전자 배열, 각 원본의 해시, 차이 배열, 적용한 물리량과 정규화, 소스 해시와 검증 상태가 포함된다. 실행 가능한 개발 코드이며 동결된 v0.16.1rc2 설치 패키지의 기능은 아니다.

```python
from camera_platform.physics_lut_bridge import (
    inspect_fdtd_lut, compare_fdtd_field_raw,
    inspect_tcad_db, compare_tcad_collection_raw,
)

optical = inspect_fdtd_lut()
field = compare_fdtd_field_raw(size=(8, 8), exposure_s=0.004, wavelength_nm=550)
baseline_raw = field["baseline"]["raw"]["values"]
changed_raw = field["with_lut"]["raw"]["values"]

collection = inspect_tcad_db()
tcad = compare_tcad_collection_raw(case="edge20x", wavelength_nm=550)
```

FDTD 경로는 저장 LUT의 **같은 파장 중심 대비 주변부 Si 광흡수 응답 비율**을 방사형 근사로 적용한다. 기존 센서 QE는 유지하며 절대 흡수율을 QE로 대체하거나 곱하지 않는다. 기본 저장 LUT는 450/550/650 nm의 center/cra10x/edge20x 요약 행은 있으나, 공간 crosstalk kernel에 필요한 pixel-region long 행과 수렴 보고는 없다. 따라서 `crosstalk_applied=False`이고 근거는 광학 field proxy로 제한된다. `wavelength_nm`은 저장 LUT의 파장 중 하나와 정확히 일치해야 하며, 주변 조건을 말없이 가장 가까운 다른 파장으로 바꾸지 않는다.

TCAD 경로는 `collected current / FDTD generation current`의 중심 대비 선택 case 비율만 적용한다. 이 값은 **한 파장의 단일 스칼라**로 모든 CFA 사이트와 픽셀에 적용되는 기존 모델의 민감도이며, 전기 수집의 공간·분광 모델이나 제품 특성은 아니다. 기본 accuracy gate는 framework-ready이지만 accuracy-ready가 아니다. FDTD와 TCAD는 이 경로에서 서로 곱하지 않는다.

두 경로 모두 기본 `pyisetcam` 센서를 사용한다. 저장 sensor-simulation DB의 `db_estimate_v1` 또는 `physical_estimate_v1`에는 자체 광학 응답이 들어 있으므로 별도 LUT를 덧붙이면 중복 계산이 된다. 기존 `system_faca`도 그 합성을 거부한다. 이 제한을 해제하려면 센서 revision과 LUT의 구조·파장·각도·정규화·전하 수집이 같은 물리량을 기술한다는 명시적인 계약과 검증이 먼저 필요하다. 이 도구의 출력은 **LUT 자체가 RAW를 얼마나 바꾸는지**를 보여주며, 센서 DB revision 순위나 FDTD 기반 제품 정확도를 주장하지 않는다.

API/Worker 경로는 `GET /api/v1/physics-lut/template`에서 기본 자산 검사와 두 층의 요청 틀을 읽고, `acknowledge_proxy_limits=true`를 명시해 `POST /api/v1/physics-lut`에 제출한다. `/api/v1/jobs/{id}`에서 공통 상태를 기다리고 `/api/v1/physics-lut/{id}`에서 결과를 읽는다. 요청은 정확히 한 LUT 층을 고른다. FDTD의 `case`는 `relative_field`, TCAD의 `case`는 실제 저장된 비중심 case다. 단일 8–32 픽셀 평탄 `uniform ee` 입력과 0–1초 노출, `noise_flag=0`으로 묶어 잡음이 차이를 가리지 않도록 한다. Worker는 제출 시의 소스 파일·실제 로드한 응답·실행 환경을 다시 확인하고, 두 RAW 원본을 각각 `.npy` blob으로 보존한다. API 소스는 workspace 아래에 있어야 하며, symlink와 sidecar의 실제 위치도 검사한다.

현재 기본 저장 자산에서 같은 8×8 장면, 4 ms, seed 0을 실행한 개발 smoke에서는 평균 RAW DN이 기본 119.5에서 FDTD field 적용 123.8125, TCAD edge20x collection proxy 적용 286.0으로 바뀌었다. TCAD center 대비 edge 효율 비율은 약 3.9995다. 이 값은 원본 자산과 가정이 특정된 **수치 민감도 예시**일 뿐이며, 실측 정확도나 제품 성능을 뜻하지 않는다.
