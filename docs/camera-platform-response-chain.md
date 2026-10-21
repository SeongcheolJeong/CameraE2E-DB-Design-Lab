# 광학·픽셀·전기 두 조건 비교 — 2026-09-26

`response_chain.evaluate_response`는 기존 `spatial.photoelectron_rate`와
`electrical.capture_electrons`를 연결한다. 공급 PSF/방향/EQE를 실제 적용하고,
광전자율에 노출을 한 번 적용한 뒤 독립-reset 전기 readout에서 정수 RAW를 만든다.
gain은 전기 readout에만 적용한다. 광입력·광전하에 곱하거나 ISP를 암묵 적용하지 않는다.
시간 트랩·HDR·실측 보정·모델 등록/승격은 이 함수의 범위가 아니다.

`field_radiometry_mode=source_position_ri_v1`을 선택하면 이 함수도 실제
`spatial.photoelectron_rate` 경로와 동일하게 **소스 위치 상대 조도 → PSF →
수신 방향 응답** 순서로 계산한다. 결과의 `processing_order`는 선택된 순서를
기록하고, `response_identity.field_radiometry_mode`가 기존 수신 위치 모드와
구별한다. `response_identity`는 native crop/수신 위치를 결정하는 sampling과
파장 축·원본 출처도 별도로 결합한다. 보존 서브셀 입력이 있으면 native 평균 영상의 해시와 별도로 원본
서브셀 `float64` 배열의 바이트 해시·shape·근거 해시를 기록한다. 동일한 native
평균 영상이라도 서브셀 배치가 다르면 서로 다른 광학 입력이다. 이 식별자 보완은
등록된 모델의 물리 정확도나 독립 실측 판정은 아니다.

`isp_profile`을 명시하면 동일한 함수가 **획득한 정수 RAW**를 기본 software ISP v1의
BLC→demosaic→고정 WB→CCM→tone으로 처리한다. 원본 `raw_dn`과 별도
`rgb_frames` 및 중간 배열을 반환하며, ISP 요약에는 원본 RAW 바이트 해시와
native crop/CFA 위상이 남는다. 프로필을 바꿔 같은 RAW를 실제 재처리하려면
저장된 `raw_dn`을 `isp.process_raw`에 전달한다. `evaluate_response` 재호출은
광학·전기 계산을 다시 수행하므로 재처리로 세지 않는다. 프로필을 생략하면
이전처럼 RAW에서 끝난다.
이 연결은 색 정확도나 HW ISP 일치를 주장하지 않는다.

## 실제 계산한 비교

`tools/run_camera_response_comparison.py`는 새 Prysm PSF 두 개와 네 사이트 EQE 두 개,
gain 1/2, 분광조도 두 수준으로 **16개 실제 응답 계산**을 수행한다.
초점거리 16 mm, F/4·F/8, 수신 pitch 2 µm, 32×32, 500/600 nm, 노출 4 ms다.
두 픽셀의 정상 입사 EQE는 같고 각도 손실만 다르다. Gr/Gb를 합치지 않는다.

균일 원형 동공·근축 1/N² 광량·분리된 telecentric 입사각 분포는 명시적 합성 가정이다.
장면은 이미 on-axis 광량이 반영된 sensor ideal plane이며 조리개 광량은 여기서만 곱한다.
정규화 PSF에는 광량을 다시 곱하지 않는다. 수신 픽셀 aperture도 PSF에 한 번만 포함된다.
방향 계약의 해시 슬롯은 작성한 해석 surrogate 식별자이지 실측/광선 추적 처방 파일의 해시가 아니다.
이 연구 자산은 Registry에 게시하거나 기존 자산의 유효 범위를 넓히지 않았다.

## 독립 기준과 결과

PSF 생성: 연속 Airy intensity의 Gauss–Legendre 픽셀 적분.
PSF 적용: 생산 FFT 함수를 호출하지 않는 직접 kernel 합산.
픽셀: 균일 동공의 평균 입사각 해석식과 작성 EQE 식.
전하: 파장 사다리꼴·면적·노출 적분. 전기: clipping/ADC 반올림의 독립 수식.
PSF 적용 기준에 공급 kernel을 쓰는 것과 kernel 자체의 독립 검증을 구분한다.

F/4·F/8의 Airy 정규화 kernel 최대 L1 차이는 각각 0.003211·0.001178이다.
전하의 최대 상대 차이는 1.15418e-7이고, 16조건의 독립 최종 RAW 차이는 0 DN이다.
Kernel OTF(픽셀 적분·유한 범위 정규화 포함)는 0.125 cycles/pixel에서
500 nm 0.83687→0.68318, 600 nm 0.80024→0.62504로 낮아진다.
이 값은 렌즈 단독 MTF 또는 실측 SFR가 아니다.

넓은 각도 응답은 F/4에서 Gr 전하를 15.706%, F/8에서 7.158% 늘린다.
그러나 밝은 장면 F/4 gain1의 well 포화율은 4.88→10.25%로 증가한다.
gain2로 올려도 전하는 같고 최종 RAW 상단 비율은 각각 35.25%·37.40%로 커진다.
F/8은 포화를 줄이지만 광량 손실과 회절 확대가 있다. 단일 '좋은 후보'로 자동 승인하지 않는다.

`adc_high_mask`는 반올림 전 연속코드 경계다. 최종 정수 `RAW==4095`와 같지 않을 수 있다.
이번 저장 RAW 해석은 둘을 분리하며 `adc_high_mask==0`을 포화 부재로 사용하지 않는다.

## 실행·결과

프로젝트 루트에서 새 결과 폴더를 지정한다. 기존 출력 폴더는 덮어쓰지 않는다.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src OPENBLAS_NUM_THREADS=1 \
  .venv-platform/bin/python tools/run_camera_response_comparison.py \
  --output reports/camera_platform/response-comparison-new
```

[설계 해석](../reports/camera_platform/response-comparison-20260926/interpretation.md) ·
[전체 비교](../reports/camera_platform/response-comparison-20260926/report.md) ·
[시각 비교](../reports/camera_platform/response-comparison-20260926/comparison.html).
`results.json`, `rail-readback.json`, 후보별 입력/중간배열/RAW, 독립 Airy 배열도 보존한다.
관련 신규 시험은 9개 통과했다. 기존 기능의 광범위한 Worker/브라우저/재시작 회귀는 반복하지 않았다.
실물 정확도·누화·일반 시야 수차·PSF/방향의 결합 상관·잡음·온도·대형 해상도는 미검증이다.

다음은 같은 비교기를 기존 처방 Factory의 두 광학 조건에 연결하는 것이다.
완료한 PWM 연구/재처리 또는 이번 이상 모델의 같은 실험을 다시 다음 단계로 반복하지 않는다.
