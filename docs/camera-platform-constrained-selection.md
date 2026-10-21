# 처방·노출·gain 제약 선택과 미사용 입력 평가

`constrained_selection.py`는 이미 계산한 후보의 최소 신호·예측 SNR·well/RAW 포화·
노출 상한을 함께 적용하는 유한 격자 선택기다. 제약을 만족한 후보 중 최악 입력의
green SNR 하위 10% 분위가 큰 것을 선택한다. 동률은 짧은 노출, 낮은 gain, ID 순이다.
해가 없으면 `no_feasible_candidate`와 `selected_candidate_id=None`을 반환하며
자동 승인하거나 적용 범위를 바꾸지 않는다. 새 서버·사전검사 API는 추가하지 않았다.

## 이번에 고정한 연구 브리프

고객 요구나 특정 CIS 데이터시트가 아니라 계산 전에 명시한 연구용 기준이다.

| 기준 | 값 |
|---|---:|
| 최소 green 평균 신호 | 120 DN, black-relative |
| Green 픽셀 예측 SNR p10 | 10 이상, 선형비 |
| Well 포화 / 최종 RAW 상단 | 각각 1% 이하 |
| 노출 상한 | 4 ms |

SNR p10은 픽셀별 SNR의 10분위가 아니라 **10% 분위수**다. SNR은 생성 광전하와
모델의 shot/dark/read/post-gain/양자화 잡음으로 계산한다. 이미지의 공간 분산을
잡음으로 사용하지 않으며, DN만 증폭해 후보를 좋게 보이게 하지 않는다.
고정 패턴 분산이 있는 모델은 이 좁은 예측식에서 거부한다. 물리 파라미터 불확실도는
포함되지 않는다. 별도 seed 반복 RMS는 사후 표본 진단이며 실측 SNR이 아니다.

## 선택과 평가의 분리

1. `plan.json`에 브리프·후보·선택 입력·hold-out 변환·seed·구현 hash를 먼저 저장한다.
2. 같은 과거 처방의 clear/quarter/radial, 노출 1/2/4/8 ms, gain 1/2/4의 36점 중
   8 ms 9점은 노출 상한으로 제외한다. 나머지 27점을 광입력 배율 0.5/2/6에서 계산한다.
3. `selection.json`을 배타적으로 생성해 후보를 고정한 뒤에만 hold-out 입력을 구성한다.
4. 미러 0.75배·90도 회전 3배·대역별 0.6/1.4배를 적용한 2배 입력과,
   4×4 국부 하이라이트 20배 스트레스를 평가한다. 평가 후 재선택하지 않는다.

여기서 `train`은 ML 학습이 아니라 **후보 선택에 쓰는 입력 분할**이다.
Hold-out은 동일 모델의 별도 이상영상 변환이지 독립 실물·독립 광학 솔버가 아니다.
범위 내/밖 구분은 **선택 입력의 대역별 최대 광량** 기준이다. 모든 공간 분포나
연속 광량 범위에서 정확하다는 뜻은 아니다. 국부 20배 스트레스는 그 상한 밖이다.

원래 투과 조건별 입력의 E/L은 이미 한 번 반영돼 있다. 각 조건에 동일한 공간/파장
연산을 가하므로 공통 변경 광휘를 유지하며, 새로운 3D 렌더링이라고 기록하지 않는다.
기존 자산의 범위·광선·PSF·픽셀·전기 모델·ISP는 변경하지 않는다.

## 실제 결정

36점 중 적합 5점, 선택은 **radial-e4-g1**이다.

| 후보 | 최소 Green DN | 최악 예측 SNR p10 | 선택 입력 최대 well/RAW 상단 |
|---|---:|---:|---:|
| radial · 4 ms · gain 1 | 196.781 | 18.728 | 0% / 0% |
| clear · 2 ms · gain 1 | 143.391 | 15.978 | 0% / 0% |
| radial · 2 ms · gain 2 | 196.781 | 13.174 | 0% / 0% |
| clear · 1 ms · gain 2 | 143.391 | 11.219 | 0% / 0% |
| quarter · 4 ms · gain 2 | 143.391 | 11.219 | 0% / 0% |

기존 clear·4 ms·gain1은 선택 입력 6배에서 well과 RAW 상단 3.125%로 탈락했다.
Quarter·2 ms·gain4는 밝기 143.391 DN은 충족하나 SNR p10 7.819로 탈락했다.
Gain만으로 광자 정보의 부족을 해결하지 못한다는 실제 선택 결과다.

선택 후보는 범위 내 3입력 × (잡음 OFF 1회 + 새 seed 1901..1904) **15/15**에서
기준을 만족했다. 국부 20배 강조광에서는 5/5가 실패했으며, 잡음 OFF well/RAW 상단은
2.734375%였다. 같은 조건의 기존 기준은 7.421875%다. 낮아졌어도 1% 요구 미달이므로
확대 사용을 승인하지 않는다. 광량 연속 범위나 확률 보증으로 해석하지 않는다.

**선택은 위 브리프에만 조건부다.** 노출 2 ms의 clear 대안도 적합하며 더 짧다.
반경별 감쇠 자산의 색 비율 변화는 이전 비교에서 확인됐지만 이번 목적함수에는
색 정확도·모션 블러·MTF 손실을 넣지 않았다. 그러므로 이 결과를 최종 렌즈 추천이나
상용 카메라 승인으로 사용하지 않는다. No-filter/원본 RAW 근거는 그대로 유지한다.

## 사용과 결과

```python
from camera_platform.constrained_selection import SelectionLimits, select_candidate

decision = select_candidate(candidates, training_rows,
    training_scenario_ids=["train-L0.5", "train-L2", "train-L6"],
    limits=SelectionLimits())
```

Hold-out, 미선언 입력, 누락 셀, 중복 셀, 후보와 다른 설정은 선택 함수에서 거부한다.
재실행할 때는 원본 결과를 덮어쓰지 않는 새 폴더를 지정한다.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv-platform/bin/python \
  tools/run_camera_constrained_selection.py \
  --output reports/camera_platform/constrained-selection-new-study
```

[결과표](../reports/camera_platform/constrained-selection-20260926/report.md),
[원본·포화 비교 화면](../reports/camera_platform/constrained-selection-20260926/comparison.html),
[사전 계획](../reports/camera_platform/constrained-selection-20260926/plan.json),
[고정 선택](../reports/camera_platform/constrained-selection-20260926/selection.json),
[전체 수치](../reports/camera_platform/constrained-selection-20260926/results.json).

새 직접 Runtime 계산 121건(선택 81 + hold-out 40), 기존 Factory 6건 재사용이다.
잡음 OFF 89건은 별도 직접 합산 광전하와 ADC 기준식으로 대조했다.
관련 29개 시험이 통과했다. Worker/브라우저/설치/실측 검증은 반복하지 않았다.
