# 기존 처방 자산 → 공통 전하/RAW 비교

개발 소스 `0.16.1.dev19` 작업 트리에 `prescription_response.ArchivedPrescription`을
추가했다. 기본 앱과 등록 모델은 바꾸지 않는다. 선택한 기존 처방 자산의 spectral
입력, field PSF, RI/각도 응답을 `response_chain.evaluate_response`에 연결한다.

## 이번에 실제 사용한 자산

`reports/camera_platform/stop-transmission-v016`의 clear/quarter/radial run과
`outputs/camera-platform-stop-v016-qa/blobs`를 읽었다. 같은 설계 자산의 구면 처방,
동일 sensor, 동일 원본 장면 render를 공유한다. 기준 초점거리 19.812067 mm,
stop 반지름 0.5 mm, 500/600 nm이며 128×128 센서의 원점 [56,56]에서 16×16 crop이다.
실물 개체가 없는 합성 설계다. 실제 판매 렌즈, vendor sensor 또는 실측 데이터라고
해석하지 않는다. 기존 RayOptics/Prysm Factory 6개를 재사용했고 새로 추적하지 않았다.

아카이브 run의 SHA-256은 과거 evidence-summary 값과 비교한다. 읽는 blob의 길이와
해시를 검사하며, PSF/렌즈/센서는 선택된 모델 payload와 비교한다. 부모 근거도 들어
있는 아카이브에서 동일 이름의 파일을 전부 같은 원본으로 취급하지 않는다. 해당
scene의 첫 published spectral image와 run의 ideal irradiance가 같아야 한다.
광량 Factory와 PSF Factory의 정규화 처방 해시·설계 자산을 맞춘다.

현재 소스의 cold 재사용 검사는 여기서 한 단계 더 나아간다. 일치하는 정규화 처방
요청마다 원본 `characterization.json`이 정확히 하나 있어야 하며, 원본 Factory
`model_candidate` 전체를 Lens의 절대 E/L·RI·입사각 가중치와 PSF에 각각 대조한다.
Factory summary의 E/L 및 기하학적 F-number도 대조한다. 아카이브의 assembly
조리개와 Lens pupil 조건은 동일해야 하고, 유한 물체면 v5에서는 거리·참조 배율이
원본 처방·Lens 응답·assembly 조건에 정확히 일치해야 한다. `optical_response`
범위에서는 명시적으로 독립인 노출·gain만 고정 광학 조건 검사에서 제외한다.
이로써 처방 입력만 같고 옛 광량/RI/각도/PSF 출력을 섞은 아카이브는 거부한다.
원본 보존 바이트의 내부 일관성 검사이며 Factory를 재실행하거나 측정 정확도를
검증하지는 않는다. 기대 run 해시는 신뢰 가능한 외부 evidence summary에서
제공되어야 한다.
이 읽기 전용 bridge는 원본 Factory 입력·결과 blob을 함께 보존한 아카이브를
대상으로 한다. 기존 stop-transmission clear/quarter/radial 기록은 이 조건을
충족한다. 과거 finite-plane HTTP run JSON에는 그 blob closure가 없으므로 이
bridge로 불러올 수 없다. 유한 조건 거부 시험은 독립적인 작은 보존 아카이브
fixture로 검증했으며, 해당 과거 run을 재구성했다는 뜻은 아니다.

조건 간에는 stop transmission을 제외한 처방을 유지한다. 각 projected image를
자기 조건의 축상 E/L로 나눴을 때 같은 광휘여야 한다. 입력에 이미 적용된 E/L을
추가 T 또는 F-number로 두 번 적용하지 않는다. 서로 다른 조건의 irradiance를
같은 값으로 억지로 고정하지도 않는다.

## 계산 및 평가

광입력 1/8배, 조건 3개, fixed/gain-match/exposure-match를 비교했다. clear는 fixed만
실행하여 같은 계산을 반복하지 않았다. 각 설정은 noiseless 2개 + nominal의 4개
seed로 총 42개 전기 취득이다. 보상 계수는 nominal green 광전하를 clear에 맞춘
연구용 값이며, adaptive AE나 등록 모델 적용 범위 승인으로 취급하지 않는다.

독립 reference는 작은 native crop의 source-pixel 직접 합산, 별도 field 보간,
theta/QE 가중합, 파장/면적/노출 적분, 독립 ADC 반올림/clipping 식이다. Runtime의
convolution/적분 helper를 기준으로 재호출하지 않는다. 공급된 PSF/각도 표 자체의
정확도나 일반 공간·각도 결합 수송은 이 대조로 검증되지 않는다.

과거 `spatial.mean_electrons`는 암전류 추가 전 광전하다. 새 경로의
`mean_photocharge_e`와 비교하며 `mean_total_charge_e`와 혼동하지 않는다.
최초 시도에서 이 비교량을 잘못 선택한 실패 기록은 별도 실패 폴더에 남겼다.
허용 오차를 늘리거나 암전류를 지우지 않고 같은 물리량으로 수정했다.

## 이번 설계 결론

25% 투과 + gain 4는 Green 573.516 DN을 복원하지만 광전하가 1/4로 남고 반복 잡음이
14.582→28.855 DN으로 증가했다. 노출 4→16 ms 보상은 광전하/잡음 수준을 회복하지만
노출 비용이 있다. 밝은 장면에서는 gain 보상도 최종 RAW 상단 51.5625%를 다시 만든다.
이때 well 포화는 0%이므로 well/ADC/최종 RAW 상단을 분리해야 한다.

파장 의존 radial 감쇠는 Green을 맞춰도 R/G 0.86340→0.78411, B/G 0.58962→0.64673의
차이가 남았다. 스칼라 gain이나 노출로 spectral 응답 차이가 복원되지는 않는다.
이 값은 합성 장면 채널 비율이지 물리 색 정확도 평가가 아니다.

기존 광전하 3개 최대 차이 0 e, 독립 광전하 기준 최대 상대 차이 2.17e-15,
noiseless RAW 14개 모두 독립 ADC와 차이 0 DN이다. 관련 시험 16개 통과.
실물 정확도·동적 blur·센서별 gain 지원·등록/설치 자격 검증은 포함하지 않는다.
과거 자산은 RGB/theta 모델이므로 Gr/Gb 비대칭이나 phi 의존성을 임의로 만들지 않았다.

## 실행과 결과

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-platform/bin/python tools/run_camera_prescription_comparison.py \
  --output reports/camera_platform/prescription-response-new
PYTHONDONTWRITEBYTECODE=1 .venv-platform/bin/python tools/run_camera_prescription_comparison.py \
  --summarize-existing reports/camera_platform/prescription-response-new
```

새 출력 폴더만 허용하고, 과거 원본/결과를 덮어쓰지 않는다. 이번 결과는
`reports/camera_platform/prescription-response-20260926-final`이다.
`comparison-detailed.html`은 서버 없이 원본 RAW, well 포화, 최종 RAW 상단을 비교한다.
원본 RAW의 표시 범위는 모두 64–4095 DN이며 노출별 자동 대비는 적용하지 않는다.
반복 잡음은 같은 frame/pixel의 seed 간 분산이며 네 seed의 표본 진단이다.

다음 작업은 이 결과를 재검증하는 반복이 아니라, 고정한 광입력 범위에서 최소 신호,
포화율, 노출 상한을 같이 만족시키는 후보 선택 및 별도 hold-out 광입력에서의 평가다.
정적 노출 비용을 실제 motion blur 검증으로 대체하지 않는다. RAW 시간 필터도 여전히
별도 opt-in이며 이 광학·전기 비교에는 적용하지 않았다.
