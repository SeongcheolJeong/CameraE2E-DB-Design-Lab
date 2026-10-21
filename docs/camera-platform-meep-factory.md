# Meep 평면층 광학 Characterization 계약

`meep_factory.py`는 실제 Meep로 정상 입사 평면층의 반사·투과·광학 흡수 분율을 계산합니다.
반환값은 **광학 Characterization 근거**입니다. Sensor Model, 수집 전자 QE, 전하 수송,
Full well, Dark current, Read noise 또는 상용 제품의 검증 결과를 만들지 않습니다.

## 실행 경계

지원 Backend는 serial CPU Meep 1.33.0이며, Python 3.10에서 독립 실행할 수 있습니다.
플랫폼 Python은 `validate_request()`와 `request_template()`을 Meep 없이 Import할 수
있습니다. 실제 계산은 운영 환경의 `CAMERA_PLATFORM_MEEP_PYTHON`으로 선택한 Interpreter와
고정된 `meep_factory.py`를 실행하는 구조입니다. 작업 JSON에는 실행 파일이나 Script 경로를
받는 필드가 없습니다. API/Worker의 환경 선택·취소·시간 제한은 상위 실행 서비스 책임입니다.

현재 호스트에서 확인한 환경은 `/Users/seongcheoljeong/FDTD/.meep-env/bin/python`,
Python 3.10.20, Meep 1.33.0, NumPy 2.2.6, MPI 비활성/프로세서 1개입니다. 플랫폼 venv에는
Meep가 없으므로 이를 같은 Python 환경으로 오인하면 안 됩니다.

```sh
/Users/seongcheoljeong/FDTD/.meep-env/bin/python src/camera_platform/meep_factory.py --probe
/Users/seongcheoljeong/FDTD/.meep-env/bin/python src/camera_platform/meep_factory.py --request request.json --output new-output.json
```

`--probe`는 Backend·Python 버전과 Adapter/Meep Python 모듈/컴파일된 `_meep` 확장 파일의
해시를 JSON으로 반환하며 계산을
실행하지 않습니다. 출력 파일은 새 경로만 허용합니다. Backend 누락·잘못된 버전·잘못된 입력은
실패 종료하며, 계산이 끝났지만 수치 조건을 충족하지 못한 결과는 `INCONCLUSIVE`로 보존합니다.
버전과 해시는 외부 배포 파일의 서명이나 모든 연결 라이브러리의 재현 가능성을 보증하지 않습니다.

## Launcher에 Interpreter 저장

아래 경로를 설치된 serial Meep Python의 절대 경로로 바꿔 한 번 실행합니다.

```sh
python tools/start_camera_platform.py --meep-python /absolute/path/to/python --no-open
```

Launcher는 저장 전에 고정 Adapter의 `--probe`로 환경을 확인하고 상태 디렉터리의
`runtime-config.json`에 선택을 보존합니다. 이후 같은 상태 디렉터리를 사용하는 Finder
실행은 이 설정을 다시 사용합니다. 배포 Launcher에 이 호스트의 Interpreter 경로가
하드코딩되는 것은 아닙니다.

`CAMERA_PLATFORM_MEEP_PYTHON`이 설정되어 있으면 저장된 경로보다 우선합니다. 환경변수와
명시한 `--meep-python`이 서로 다르면 거부하며, 기존 서버가 다른 Meep 환경으로 실행 중인
경우 설정만 바꾸어 실행 환경이 전환된 것처럼 표시하지 않습니다. 해당 서버를 종료한 뒤
선택한 환경으로 다시 시작해야 합니다. `--no-open`은 브라우저 자동 열기만 생략합니다.

## 입력

`request_template()`은 아래와 같은 합성 유전체 예제를 반환합니다. 실제 CFA나 특정 Sensor의
특성표가 아닙니다. 모든 객체는 정의된 필드만 허용하며 숫자 문자열·Boolean·NaN·Infinity는
변환하지 않고 거부합니다. JSON 중복 키도 거부합니다.

```json
{
  "schema": "meep_planar_stack_v1",
  "wavelength_nm": [550.0],
  "ambient_index": 1.0,
  "incidence_deg": 0.0,
  "polarization": "linear_x",
  "layers": [{
    "name": "Declared synthetic lossy dielectric",
    "thickness_nm": 200.0,
    "optical_constants": {"n": 2.0, "k": 0.1},
    "provenance": {
      "source_kind": "estimated",
      "source_uri": "fixture://planar-dielectric-n2-k0.1",
      "license": "MIT",
      "method_and_version": "declared_uniform_material_v1"
    }
  }],
  "simulation": {
    "resolution_px_per_um": 80,
    "pml_thickness_um": 0.8,
    "air_gap_um": 1.0,
    "after_source_time": 40.0,
    "source_bandwidth_fraction": 0.2,
    "convergence_tolerance": 0.01
  }
}
```

파장별 물성은 `optical_constants`를 다음 형식으로 바꿉니다. 파장 배열은 요청 배열과 정확히
같아야 하며, 최근접 선택·보간·범위 밖 외삽을 하지 않습니다.

```json
{"wavelength_nm": [450.0, 650.0], "n": [2.0, 2.1], "k": [0.08, 0.12]}
```

| 항목 | 제한과 의미 |
| --- | --- |
| 입사/형상 | +z 정상 입사, Ex 선편광, x/y로 무한한 균질·등방성 평면층, 양쪽 공기 n=1 |
| 파장 | 진공 파장 nm, 350–1100 nm에서 명시한 1–5개 오름차순 고유 표본 |
| 층 | 입사 순서대로 1–8개, 각 두께 20–5000 nm, 총 10 µm 이하, 고유 이름과 개별 출처 |
| 물성 | 무차원 n=1–6, k=0–2, n²−k²>0; 이득·금속·음의 실수 유전율·비등방성 미지원 |
| 격자 | 기준 16–400 pixel/µm 정수, 각 층 ≥4 cell, 물질 내부 파장 ≥8 cell |
| PML/여유 공간 | 기준 PML 0.3–3 µm, 양측 air gap 0.6–3 µm |
| 시간 | Source 종료 후 20–200 Meep 시간 단위, 단위 µm/c |
| Source | 중심 주파수의 0.1–0.3 배 Gaussian bandwidth |
| 판정 | 절대 분율 차이 0.0001–0.02, 기본 0.01; 별도 고정 passivity 수치 여유 10⁻⁶ |
| 계산량 | 파장당 8개 1D 계산, 전체 추정 cell-step 1억 이하; JSON 입력 256 KiB 이하 |

격자·계산량 최소 조건은 실행 전 거부 기준이며 수렴을 보장하는 기준은 아닙니다. 고해상도와
많은 파장을 동시에 요청하면 계산량 제한에 걸릴 수 있습니다. Meep의 시간/주파수는
선택한 길이 단위 a=1 µm와 c=1을 기준으로 하므로 주파수는 `1000 / wavelength_nm`입니다.
[Meep 단위 설명](https://meep.readthedocs.io/en/latest/Introduction/#units-in-meep).

각 파장에서 ε′=n²−k², ε″=2nk를 계산하고 σD=2πfε″/ε′를 사용합니다. 이 전도도 표현은 해당
주파수의 물성을 맞추는 좁은 대역 모델입니다. 한 번의 광대역 실행에서 선언한 n,k 전체가
재현된다고 주장하지 않으며, 파장마다 별도 계산합니다.
[Meep 물성/전도도 설명](https://meep.readthedocs.io/en/master/Materials/).

## 정규화, 부호와 수렴 판정

각 파장마다 기준 설정, 해상도만 2배, PML 두께만 1.5배, Source 종료 후 시간만 2배인
네 설정을 실행합니다. 각 설정은 자체 공기 Reference와 실제 Stack 계산을 갖습니다.
여러 파라미터를 동시에 바꾼 두 결과를 독립된 수렴 시험으로 세지 않습니다.

공기 Reference의 양의 +z 입사 Flux를 정규화 분모로 보존합니다. 반사는 Reference의
DFT 필드를 빼고 얻은 역방향 Flux의 부호를 바꿉니다. 투과는 출사면의 +z Flux입니다.
흡수는 Stack 경계 순서의 순 Flux 차이를 합하여 계산하고 `R + T + A − 1`을 따로 확인합니다.
`A=1−R−T`로 만든 값을 다시 에너지 보존 시험으로 사용하는 순환 검증은 하지 않습니다.
[Meep Flux 정규화와 반사 계산](https://meep.readthedocs.io/en/latest/Python_Tutorials/Basics/).

바깥 흡수 Flux 면은 재료 경계에서 떨어진 공기 안에 둡니다. 내부 면은 층 사이 경계이며
Yee 격자의 보간 영향을 받습니다. 층별 분율도 해상도 변화와 passivity를 확인하며, 이 값은
깊이별 전하 수집 확률이 아닙니다. 격자에 맞지 않는 Cell 치수는 Meep가 반올림할 수 있고,
상위 실행 서비스는 해당 stderr 경고도 계산 근거로 보존해야 합니다.

원본 Flux/정규화/좌표와 음수 값은 그대로 보존합니다. 절댓값 또는 0–1 clipping으로 물리를
수리하지 않습니다. ±10⁻⁶ 내의 음수는 수치 여유로 표시하되 값은 바꾸지 않습니다.
이를 넘는 음수·상한 초과, 정규화 불일치, 에너지 잔차 또는 설정 간 변화가 기준을 넘으면
`INCONCLUSIVE`입니다. 비유한 계산값과 0/음수 Reference는 계산 실패입니다.

모든 수치 조건을 만족하면 `NUMERICALLY_CONVERGED`입니다. 이는 시험한 두 수준 사이의
변화가 지정 허용치 이내라는 뜻이며, 일반적인 오차 상한·물성 정확도·독립 실측 검증은 아닙니다.
결과의 `uncertainty`는 항상 `unknown`, `physical_validation_pass`는 false,
`approval_status`는 unapproved, `model_candidate`는 null입니다.

## 반환 및 Sensor Model Factory로 확장하는 경계

반환 객체는 `{summary, arrays, model_candidate: null}`입니다. Summary에는 요청과 해시,
Backend, 물리 기준면·효과, 수치 판정과 제한을 보존합니다. 배열 축은 configuration→wavelength,
층별 값의 순서는 입력 층 순서입니다. 각 결과에는 R/T/A, 층별 흡수, 에너지/정규화 잔차,
Reference/Stack 원본 Flux, Monitor/Source 위치 및 실제 ε/σ를 포함합니다.

RGB CFA가 필요하면 각 필터의 독립된 물성/두께 근거로 별도 Characterization을 구성해야 합니다.
한 개의 녹색층 결과를 R/B로 복제하거나 일반 CFA 특성을 추정해서 채우지 않습니다. Microlens,
입사각 응답, 측방 Crosstalk, 전하 생성 수율·수송·수집은 현재 포함하지 않습니다.

수집 전자 QE로 이어가려면 별도 전하 생성/수집 근거와 기준면·픽셀 면적·조건·파장/공간 축을
연결해야 합니다. 필요에 따라 `광학 흡수 밀도 → 생성률 → 수집 전자` 단계를 각각 고정합니다.
광자 Flux와 cm⁻³s⁻¹ 생성률을 사용할 경우 길이/면적 단위 변환 및 수율을 명시해야 합니다.
수집 효율·Full well·Dark·Noise가 없으면 unknown으로 남기고 완성된 Sensor Model로 만들지 않습니다.

## 기존 FDTD/TCAD Package와의 관계

읽기 전용 점검에서 CameraE2E에는 FDTD/TCAD Script와 `fixtures/fdtd_active_lut/camera_lut.json`
하나가 있었고, 전체 생성 Package는 `/Users/seongcheoljeong/FDTD/runs/camera_e2e_sensor_lut_package`
에 있었습니다. 2026-06-27 생성본은 7개 Sensor, 559개 Runtime 행, 147개 요구 항목을 가지며
연구용 로딩은 허용하지만 제품용 요구 항목과 Product-ready Sensor는 모두 0개입니다.
해당 상태는 현재 디스크에서 읽은 기존 산출물이며 이번 Adapter가 새로 계산한 결과가 아닙니다.

그 Package를 새 플랫폼으로 가져올 때 다음 한계를 유지해야 합니다.

- `build_camera_e2e_sensor_luts.py`의 일부 Field 응답은 전기적 lateral-shift proxy이며
  전체 광학 CRA 응답이 아닙니다. Electrical collection efficiency와 Readout 값에도 prior가 있습니다.
- `query_camera_e2e_runtime_bundle.py`는 범위 밖 Field를 clamp하고 최근접 파장을 선택하며,
  가중 평균에서 비유한 값을 제외합니다. 새 계약은 이 동작을 물리 정합으로 인정하지 않습니다.
- `meep_supercell_lut.py`는 Signed Si 흡수 진단값에 `abs()`를 적용한 값도 저장합니다.
  후속 Import는 원래 부호와 Flux 정규화 근거를 검사해야 하며 절댓값만 수용하면 안 됩니다.
- 기존 Split-PD Fixture는 녹색 채널, Proxy 물성, Chief-ray-only, 고정 시간 결과입니다.
  현재 Runtime의 방사 대칭 RGB 수집전자 QE/각도응답으로 직접 변환할 수 없습니다.
- 기존 `response_min/max`와 불확실도 prior, Gate 문자열은 실측으로 확립한 절대 오차 경계나
  원본의 암호학적 무결성 보증이 아닙니다. 원본 바이트·변환·부모 근거를 별도로 고정해야 합니다.

새 Adapter는 기존 Package나 Solver 코드를 변경하지 않습니다. 알려진 단일층 Fresnel 해석해,
공기/무손실/손실 유전체, 독립 수렴 설정, 비정상 입력과 음수 흡수 거부를 시험합니다.
이 구현 검증으로 특정 상용 Sensor의 정확도나 외부 자료의 배포권이 확보되지는 않습니다.
