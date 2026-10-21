# 보존 Native RAW의 고급 ISP · v0.16.0.dev5

개발 소스에 추가한 `camera_isp_profile_v2` 경로입니다. 기본 설치본 v0.15에는
아직 포함하지 않았습니다. 기존 v1 ISP의 프로파일과 연산은 유지하며, v2는 **이미
저장된 단일 stream의 native RAW**를 재처리합니다. 새 노출·전하·잡음을 만들지 않습니다.
검증 범위와 실제 실행 근거는 [개발 근거 요약](../reports/camera_platform/advanced-isp-v016/evidence-summary.json)에 연결합니다.

## 처리하는 문제

같은 RAW에서 주변부 셰이딩을 보정하면서 잡음·디테일·halo가 어떻게 바뀌는지 비교합니다.
LSC와 denoise는 원본 센서·렌즈·읽기 모드·조건에 결속된 보정 버전이 필요합니다.
보정 형식의 기본값은 명시적인 추정 예제이며 실측 교정 또는 자동 추정 결과가 아닙니다.

```text
보존 integer RAW → black level → native 4사이트 LSC → same-site denoise
→ bilinear demosaic → fixed WB → CCM → bounded linear sharpen → tone
```

새 세 단계는 개별 bypass가 가능합니다. WB·CCM·tone이 같은 v1 baseline도 같은 RAW에서
계산해 저장합니다. 이 비교에서 서로 다른 노출, crop, 표시 자동 정규화를 섞지 않습니다.

## 계산 모듈

[`isp_v2.py`](../src/camera_platform/isp_v2.py)는 float64 순수 처리 모듈입니다.
[`isp_calibration.py`](../src/camera_platform/isp_calibration.py)는 보정의 수치 계약과
원본·적용 범위·무결성을 관리하고, [`isp_replay_v2.py`](../src/camera_platform/isp_replay_v2.py)는
불변 제출·별도 Worker·원본 배열과 미리보기 저장을 담당합니다.

| 단계 | 현재 연산 | 보존하는 경계 |
|---|---|---|
| LSC | native pixel-center 좌표의 격자를 bilinear 보간해 signed BLC 값에 곱함 | R·Gr·Gb·B 분리, crop 원점 유지, 외삽 거부, 무효 support는 gain bypass와 invalid mask |
| RAW denoise | 각 CFA sublattice의 3×3 이웃에 공간·신호 차이 가중치 적용, 원본과 strength로 혼합 | 다른 CFA·다른 시점과 혼합하지 않음, 무효 이웃 제외, 무효 중심은 원본 유지 |
| Linear sharpen | CCM 뒤 RGB 평균 guide에서 3×3 blur를 빼고 문턱·amount·최대 추가량으로 제한 | signed 값 유지, 무효 stencil은 bypass, 최종 tone에서만 [0,1] clip |

Noise hint는 BLC 이전 DN 코드에서 black만 뺀 신호를 기준으로
`slope_dn × max(signal_dn, 0) + intercept_dn2`를 사용합니다. LSC 뒤에는 gain²와
BLC normalization²를 반영합니다. 유효 신호 범위 밖의 hint는 mask로 표시하며,
필터의 분산 가정을 비선형 처리 뒤의 정확한 covariance나 총불확실도로 해석하지 않습니다.
총불확실도는 `unknown`입니다.

LSC는 관측된 값과 잡음을 함께 키울 수 있습니다. 광학 throughput이나 사라진 정보를
복구했다고 주장하지 않습니다. Sharpen 결과의 edge/SFR도 Lens optical MTF와 구별합니다.

## 보정과 재사용

`isp_calibrations`는 새 불변 보정 버전입니다. 원본 JSON과 정규화 JSON을 각각 저장하며
참조 원본의 blob도 해시로 확인합니다. 다음 정보가 함께 고정됩니다.

- 센서·렌즈의 모델 ID와 전체 기록 해시, native full shape·pixel pitch·RAW 형식·읽기 모드
- 보정의 기준 RAW, 원본 dataset과 source kind, 계측 정보·부모 버전·불확실도
- 노출·gain·온도 등 원본의 모든 조건에 대한 명시적 exact/interval 적용 범위
- LSC grid와 유효 mask·기준 ROI·정규화, 네 사이트의 noise hint와 DN 신호 범위

다른 RAW에 재사용할 때도 같은 고정 모델·읽기 모드와 적용 범위를 확인합니다.
현재 실행할 영상에서 셰이딩이나 잡음을 자동 추정하지 않습니다. 원본·보정·프로파일
변조와 이미 적용한 보정 identity의 이중 적용은 거부합니다. `measured`라는 출처만으로
실물 검증 또는 release 승인을 부여하지 않습니다.

## API·SDK와 화면

실험 화면의 **보존 RAW 재처리**에서 원본을 선택하면 고급 ISP 편집기를 사용할 수 있습니다.
추정 보정 형식 또는 보정 JSON을 불러와 격자 노드의 4사이트 gain과 noise 값을 작성하고,
새 보정 버전을 등록합니다. 그 버전을 고정한 v2 프로파일에서 세 단계의 적용·강도를
선택해 새 모델을 등록한 뒤 기존 재처리 버튼으로 실행합니다.

| 경로 | 용도 |
|---|---|
| `GET /api/v1/templates/isp-calibration?source_kind=run&source_id=…` | 해당 원본의 명시적 추정 보정 형식 |
| `POST /api/v1/isp-calibrations` | 보정 원본·조건 검사 후 불변 등록 |
| `GET /api/v1/isp-calibrations` | 등록한 보정의 원본 무결성을 검사하여 조회 |
| `GET /api/v1/templates/isp-profile?version=2&source_kind=run&source_id=…&calibration_id=…` | 같은 원본에 적용할 v2 ISP 모델 형식 |
| `POST /api/v1/models` | ISP 모델 버전 등록 |
| `POST /api/v1/replays` | 원본 RAW와 ISP 모델을 고정한 재처리 |
| `GET /api/v1/replays/{id}/bundle` | 원본·보정·프로파일·단계 산출물을 포함한 근거 ZIP |

SDK의 `isp_calibration_template`, `import_isp_calibration`, `advanced_isp_template`,
`submit_replay`도 같은 API를 사용합니다. 기존 프로파일 template은 version을 생략하면
v1을 반환합니다. v1 요청을 자동으로 v2로 바꾸지 않습니다.

Worker는 원본·모델·보정을 다시 조회해 제출 당시 전체 입력과 대조합니다. 캐시를
재사용하기 전에도 원본과 캐시 결과 blob을 검사합니다. 근거 ZIP은 재사용한 보정의 기준 RAW와
부모 보정 계보까지 포함합니다. 결과에는 단계별 float NPY·validity mask,
gain map·noise hint, v1 baseline·v2 RGB, 표시 범위가 고정된 프레임별 미리보기가 남습니다.

## 지원 범위와 남은 작업

배열별 128 MiB·working array 추정 1 GiB, 기존 RAW frame-pixel 상한을 적용합니다.
예산을 넘는 입력은 미리 거부하며 자동 resize하지 않습니다. 이 예산은 측정 peak RSS나
실시간 처리 보증이 아닙니다. Crop 경계는 반사 방식이며, full-frame과의 비교는 실제
활성 stencil을 합친 halo를 제외한 interior에서 수행합니다.

현재 v2는 replay 전용입니다. Camera/Temporal/HDR/DCG 획득 경로에 v2를 넣으면
지원하지 않는 조합으로 표시합니다. DCG의 두 readout을 frame으로 펼치지 않습니다.
경로별 format·보정·shared-charge identity를 보존하는 추출 계약과 float HDR 입력은
후속 범위입니다.

별도 [합성 반복 영상 비교](../reports/camera_platform/advanced-isp-v016/tradeoffs/isp-tradeoffs.png)는
16회 반복한 flat·edge·texture에서 미리 정한 6개 설정을 각각 평가합니다. 잡음의 시간 잔차,
평균 편향, 경계 halo, 질감 진폭을 별도 축으로 보존합니다. 예를 들어 sharpen을 끈 상태에서
denoise를 0에서 0.8로 높이면 flat 잡음은 약 5.435에서 3.954 DN으로 줄지만 texture 진폭
비율도 약 1.012에서 0.901로 줄었습니다. 이것은 작성한 Gaussian 가정의 합성 결과이며
실제 카메라 SNR이나 광학 MTF를 입증하지 않습니다. 이 비교 도구는 아직 제품의 일반
후보 탐색·자동 최적화 기능에 통합하지 않았습니다.

보정 fitting, 시간 denoise, 제조사 HW ISP 동등성, 독립 실측 정확도, 적응형·다목적
최적화는 남아 있습니다. 이 replay는 기존 일곱 모듈과 네 실행 방식 안에서 동일 RAW의
처리 비교와 다음 실측·설계 판단에 사용할 근거를 제공합니다.

다음 [동일 RAW의 ISP 후보 탐색 계획](camera-platform-isp-studies-next-slice.md)은 미구현
제안입니다. 기존 새 촬영 기반 Study와 구분해, 후보×자료의 고정 입력·별도 검증·사례
판단 연결부터 확장합니다.
