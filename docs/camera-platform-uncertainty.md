# 입력 경계와 미확인 불확실도

v0.6은 Quick Estimate와 정적 Response-DB의 **선언된 입력 경계**를 연속 기대 광전자,
RAW 신호 DN, R/G·B/G 범위까지 전파합니다. RAW 관측 시험에서는 동일한 CFA/ROI
평균 연산 후 비율을 계산합니다. 불확실도 선언은 Nominal RAW/RGB나 난수 Seed를
바꾸지 않습니다. 결과의 `uncertainty_budget`은 사용한 모델/시험 ID·내용 해시·입력 경로,
단위·출처·계산 방법·조건부 범위와 미확인 항목을 보존합니다.

현재 구현은 **부분 전파**입니다. 선언하지 않은 입력은 조건부 계산에서만 기준값으로
고정하며, 전체 예측 불확실도에서 0으로 처리하지 않습니다. 실물 모델 불일치, 파장
사이의 적분 오차, 각도/상고 보간 오차, ADC 양자화·유한 관측 통계는 아직 경계가
확립되지 않았습니다. 따라서 전체 `uncertainty.kind`는 `unknown`이고 해당 비교는
불확실도 근거가 부족한 상태로 남습니다. 입력 경계만으로 실물 검증 PASS를 만들지 않습니다.

## 공통 선언

Model과 Test의 새 버전에 선택적으로 `parameter_uncertainty`를 넣습니다. 예를 들어
센서의 QE 각 원소에 절대 경계 0.005 electron/photon을 가정한 입력은 다음과 같습니다.

```json
{
  "parameter_uncertainty": {
    "qe": {
      "kind": "absolute_bound",
      "unit": "electron/photon",
      "value": 0.005,
      "source_kind": "estimated",
      "source_uri": "fixture://declared-qe-bound",
      "method_and_version": "authored-assumption/1"
    }
  }
}
```

이 예제는 가정이며 실측 결과가 아닙니다. `value`는 명시적 스칼라 또는 Nominal 배열과
정확히 같은 Shape여야 합니다. 스칼라는 모든 원소에 적용합니다. 음수·NaN·문자열·Bool,
잘못된 단위·형상과 지원 물리 범위를 넘는 경계는 거부합니다. 모르는 항목은
`kind: "unknown"`, `value: null`, 명시적인 `reason`으로 기록합니다. 출처 메타데이터를
입력하는 것만으로 해당 원본의 진위·계측 신뢰도·승인이 검증되지는 않습니다.

| 소유 객체 | 경로 | 단위 |
| --- | --- | --- |
| Lens Model payload | `angular_weights`, `relative_illumination` | `1` |
| Sensor Model payload | `qe` | `electron/photon` |
| Sensor Model payload | `pixel_pitch_um` | `um` |
| Sensor Model payload | `sensor_angular_response.relative_response` | `1` |
| Sensor Model payload | `readout.full_well_e`, `readout.dark_current_e_per_s` | `electron`, `electron/s` |
| Sensor Model payload | `readout.gain`, `readout.black_level_dn` | `1`, `DN` |
| Test | `stimulus.spectral_irradiance_w_m2_nm` | `W/m^2/nm` |
| Test | `conditions.exposure_s.value` | `s` |

정규화된 RI와 각도 응답의 기준점은 정확히 1이므로 그 원소의 경계는 0이어야 합니다.
노출/Gain 경계가 선택 모델의 운용 범위를 벗어나면 제출을 거부합니다. 노출 경계를
선언한 시험의 노출을 Sweep 파라미터로 바꾸려면 해당 조건과 경계를 가진 새 시험
버전을 등록합니다. 보정으로 각도 응답을 새로 피팅하면 해당 부모 입력 경계는 자동
이동시키지 않고 새 후보에서 `unknown`으로 기록합니다.

## 계산과 표시

양의 분광 광량·응답을 구간으로 계산하고, 고정 파장 축의 사다리꼴 적분, 광전자·Dark,
Full-well/Gain clipping, Black/Dark 보정 및 채널 나눗셈까지 전달합니다. 구간 산술의
기본 연산은 NumPy `nextafter`로 바깥 방향으로 확장합니다. 파장·각도·상고 좌표와
RAW Bit depth는 고정된 입력입니다. 단위 변환과 SI 상수의 소수 표현도 구간에 포함합니다.
이것은 주어진 이산 계산의 부동소수점 반올림을 다루며 연속 실물의 수치 근사 오차까지
규명하는 것은 아닙니다.

상관관계는 독립으로 가정하지 않습니다. 선언된 경계의 Cartesian 외피를 사용하므로
공통 변수의 재사용, 정규화된 각도별 광량 또는 공통 광원처럼 실제로 결합된 입력의
범위를 넓게 감쌀 수 있습니다. 특히 같은 광량이 R/G에서 상쇄되더라도 경계가 넓어질
수 있습니다. 현재 결과를 최솟값/최댓값을 정확히 찾은 결과나 민감도 순위로 해석하지 않습니다.
Green 분모 구간이 0을 포함하면 유한한 비율 경계를 제공하지 않습니다.

실험 화면의 **입력 오차가 결과에 미치는 범위**에서 조건부 구간과 미확인 항목을
함께 확인합니다. Quick Estimate에서 사용하지 않는 Lens/각도 입력은 별도로 표시합니다.
공간 PSF·시간 시퀀스는 이 경계 전파를 아직 지원하지 않으며, 해당 입력이 등록돼 있으면
`not_propagated`와 원본 선언을 결과에 표시합니다. SNR, 단일 RAW 샘플의 확률 범위,
ISP RGB 불확실도, PSF·시간 적분·일반 모델의 전파는 후속 작업입니다.

표준편차와 확장 불확실도는 확률적 의미와 분포·포함확률 조건을 갖습니다.
현재 절대 경계 판정기에 이를 자동 변환하지 않습니다.
[NIST: 불확실도 보고](https://www.nist.gov/pml/nist-technical-note-1297/nist-tn-1297-7-reporting-uncertainty),
[BIPM: JCGM 지침과 분포 전파](https://www.bipm.org/en/web/guest/publications/guides).

## 검증 근거

- `tests/platform/test_uncertainty.py`: 고정 소수 정밀도의 독립 광전자 적분식, 결합 파라미터
  표본과 포화, 각도 보간·분광 Lens, ROI 평균과 Dark 처리, 분모 0과 비정상 입력.
- `tests/platform/test_uncertainty_workflow.py`: 원본 모델 보존, 경계/출처가 고정된 Run,
  Cache·Nominal RAW 보존, 단위·형상·운용 범위 거부, 미확인 전체 경계의 승격 방지.
- [HTTP/Worker](../reports/camera_platform/uncertainty-http-verification.json): 실제 모델/시험
  등록→다섯 입력 전파→동일 RAW/RGB→재실행→RAW Import/ROI→비교 보류→근거 ZIP.
- [독립 브라우저](../reports/camera_platform/uncertainty-ui/headless-qa.json): 저장 Run과 ROI의
  실제 API 범위·원본 해시·미확인 항목, Desktop/Mobile 표시를 확인합니다.

이 단계는 원문 전체 플랫폼의 완료가 아닙니다. 다음 UQ 확장은 측정/모델/수치 기여의
독립 근거, 상관을 보존한 전파, PSF·시간·ISP 관측과 확률적 판정 정책입니다.
