# 두 고정 후보의 움직임·RAW 채널비 비용

## 이번 결정

기존 신호·예측 SNR·포화·노출 제약에서 선택한 `radial-e4-g1`과 이미 적합했던
`clear-e2-g1`을 동일 처방·센서·native 좌표에서 비교했다. 노출/gain/광학 모델은
다시 튜닝하지 않았다. 추가 연구 기준은 정적 대비의 signed motion transfer 80% 이상,
한 번 고정한 neutral 보정 후 reference RAW R/G·B/G 상대 차이 3% 이하다.
기존 최소 Green DN/SNR, well·RAW rail, 4 ms 노출 상한도 유지했다.

개발 입력에서 추가 기준을 만족한 후보는 **clear·2 ms·gain 1**이었다.
이 선택과 neutral 보정을 고정한 뒤 새 주파수·위상·속도·분광 입력 세 가지로 평가했고
선택 후보는 세 가지 모두 통과했다. 속도 스트레스 두 가지에서는 두 후보 모두
대비 기준에 미달했다. 이전 signal-only 선택 기록을 수정하지 않았다.

## 구현

- `src/camera_platform/motion_color_tradeoff.py`: box-shutter 시간 적분, signed 대비
  투영, 한 입력에서 고정하는 neutral channel multiplier 및 RAW 채널비 잔차.
- `tools/run_camera_motion_color_tradeoff.py`: 기존 처방 adapter/response chain을
  재사용하는 단일 실행 도구. 사전 계획 → 보정 고정 → 개발 비교 → 후보 고정 → 새 입력 평가.
- `tests/platform/test_motion_color_tradeoff.py`: 독립 시간 구적, 같은 중심시각,
  대비 소실·부호 반전, 고정 보정의 다른 분광 입력 실패를 검사한다.

```bash
cd /Users/seongcheoljeong/Documents/CameraE2E
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-platform/bin/python \
  tools/run_camera_motion_color_tradeoff.py \
  --output reports/camera_platform/motion-color-new-study
```

출력은 반드시 새 폴더여야 한다. 실행하지 않고 결과를 보려면
`reports/camera_platform/motion-color-tradeoff-20260926/comparison.html`을 연다.
`plan.json`, `frozen-neutral-gains.json`, `conditional-choice.json`, `results.json`,
원본 정수 RAW·입력 평균 조도·광전하·포화 마스크를 함께 보존한다.

## 수치 의미

고정된 source 창 안에서 수평으로 움직이는 정현파 이상영상 입력이다.
두 후보의 **노출 중심시각과 그 시각의 장면 위상**을 같게 둔다. global box shutter에서
평균 조도는 `1 + m*sinc(f*v*T)*cos(2*pi*(f*x+phase))`로 정확히 적분된다.
`f`는 cycles/native pixel, `v`는 native pixel/s, `T`는 seconds다.
현재 선형·시간 불변 공간 광학과 독립 reset readout에서는 먼저 조도를 시간 평균한 후
기존 PSF/각도/QE 연산을 적용해도 같은 노출 광전하를 얻는다.

이 평균에 노출 T를 **전하 적분에서 한 번만** 적용한다. 두 번째 수신 픽셀 aperture,
추가 F-number/투과율 배율, 이미지 wrap-around 또는 원본 RAW의 사후 블러를 넣지 않는다.
조건별 PSF·절대 광량·각도 응답은 이전 Factory 자산을 그대로 사용한다.

각 후보의 같은 분광 입력에 대해 flat/static/moving 취득을 따로 계산한다.
광학 경계 envelope를 flat으로 제거하고, 해당 후보의 static modulation을 기준으로
moving modulation의 signed 비율을 측정한다. **소실과 반전은 절댓값으로 숨기지 않는다.**
평가용 전하와 정수 RAW의 결과를 모두 저장하며, 포화된 RAW는 선형 대비 측정으로 승인하지 않는다.

## 관측 결과

| 8픽셀 주기의 무늬 속도 | radial·4 ms | clear·2 ms |
|---:|---:|---:|
| 500 pixel/s | 90.032% | 97.450% |
| 1,000 pixel/s | 63.662% | 90.032% |
| 2,000 pixel/s (stress) | 약 0% | 63.662% |
| 3,000 pixel/s (stress) | -21.221% (반전) | 30.011% |

위 수치는 동일 후보의 정적 대비에 대한 **광전하 영역** 상대값이다. 정수 RAW의
반올림이 있는 값은 `results.json`의 `motion.raw`에 별도 보존한다.

색 보정은 새 균일 neutral 입력 하나에서만 R/G/B multiplier를 정했다.
radial은 평가용 500/600 nm 비율 [1.6,0.4], [0.35,1.65]에서 보정 후에도
reference 대비 최대 채널비 차이가 각각 5.381%, 5.915% 남았다.
**clear·2 ms를 색비 reference로 택했으므로 그 후보의 잔차는 정의상 0이다.**
이는 clear의 절대 색 정확도를 입증하거나 Delta-E를 측정한 결과가 아니다.

## 검증과 한계

새 직접 response-chain 취득은 40건(보정·flat/static reference 포함), 새 Factory 계산은 0건이다.
기존 clear/radial의 광량/PSF Factory 네 건을 재사용했다. 시간 적분은 별도 64점
Gauss-Legendre 구적과 비교하고, 기존 독립 source-pixel/각도/QE/ADC 참조와 모든
취득을 대조했다. 관련 최종 시험 31개가 통과했다.

이는 합성 구면 처방, 두 파장 노드, 고정 창, 공급 source-cell 중심 표본과 기존 분리
PSF/각도 근사에 대한 연구다. 실제 3D 이동·가림·rolling shutter·trap 이력·온도·HW 지연·
실측 잡음·객체 인지 정확도를 검증하지 않았다. 이번 새 취득은 전부 잡음 OFF다.
noise-on 반복 검증이나 물리 샘플 시험으로 집계하지 않는다.

neutral 보정은 비교 지표에만 적용했고 ISP·등록 모델·원본 RAW를 바꾸지 않았다.
기본 앱·DB·설치본·서버를 변경하지 않았으며 HTML의 실제 브라우저 검증은 하지 않았다.

다음은 같은 연구를 재실행하는 것이 아니라, 기존 실험 작업실에 저장된 두 결정
(신호 우선 / 움직임·채널비 추가)을 읽어 요구 조건별 근거와 실패를 나란히 표시하는 연결이다.
이 결과만으로 자동 배포하거나 인지 원본을 가공 영상으로 대체하지 않는다.
