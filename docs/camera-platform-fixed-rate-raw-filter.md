# 50 fps 인과적 RAW 처리 후보 — 2026-09-26

`src/camera_platform/raw_temporal.py`는 독립적으로 선택하는 처리 모듈이다.
기존 등록 ISP 프로필이나 기본 앱에 자동 적용하지 않는다. 입력은 정수 Bayer RAW,
black/white code, 시간과 취득 설정 식별자다. 출력은 **처리된 float64 DN**이며
새 센서 취득 RAW, 광전자 복원 또는 물리 LFM 보증이 아니다.

## 알고리즘과 시간 비용

현재 프레임과 직전 세 개의 **원본**을 사용한다. (t,t-2), (t-1,t-3)의 코드가
선언된 허용오차 안에서 일치하고 인접 프레임 차이가 제한 이내일 때만 t와 t-1을
동일 픽셀 위치에서 평균한다. CFA 2×2 셀 내 어느 사이트든 변화/rail에 걸리면
그 셀 전체를 현재 RAW로 통과시킨다. 서로 다른 CFA 사이트의 값을 섞지는 않는다.
세 개의 초기 프레임과 설정·간격·크기 변경 직후에는 평균하지 않는다.

미래 프레임을 기다리지 않고 20 ms 간격을 유지하지만, 평균한 픽셀에는 과거
20 ms의 정보가 50% 섞인다. 명목 정보 나이는 10 ms다. 무지연 처리라고 부르지 않는다.
noise sigma는 처리 설정이며 센서 내부 전하/트랩 truth로 추정하지 않는다.

```python
from camera_platform.raw_temporal import CausalRawPairFilter, PairFilterConfig

processor = CausalRawPairFilter(
    black_level_dn=64, white_level_dn=4095, cadence_ns=20_000_000,
    config=PairFilterConfig(noise_sigma_dn=14.0),
)
# raw_frame: 실제 취득의 integer [row,column]. settings_key는 exposure/gain,
# readout/CFA/PD-clear/시간기준 변경에 맞추어 갱신한다. 별도 취득은 reset().
result = processor.push(raw_frame, timestamp_ns=frame_timestamp_ns,
                        settings_key=acquisition_settings_identity)
processed_dn = result["code_dn"]
blend_mask = result["blend_mask"]
```

예시 숫자는 이번 작성 사례 전용이다. 다른 센서의 black/white/잡음/취득 간격을
이 값으로 가정하지 않는다. 셀 밖으로 이동하는 실제 물체의 추적·정합은 하지 않는다.

## 실제 비교와 판정

원래 8×8·16프레임·노출 8 ms·촬영 20 ms·gain 1·trap 조건을 유지했다.
정적 입력 16건은 이전 연구의 저장된 RAW를 재사용했다. 밝기 감소·증가·움직이는
경계는 위상 4개 × 잡음 OFF/ON(seed 823) = 24건을 새 Runtime으로 계산했다.
실제 장면 밝기를 두 프레임 주기로 바꾼 반례 1건도 별도 계산했다.
따라서 **새 계산 25건/400프레임, 재사용 16건/256프레임**이다.

정적 원래 위상(잡음 OFF)의 후반 변화폭은 무처리 4.06881%, 단순 평균과 새 필터
0%였다. 새 필터의 밝기 증가/감소 첫 변화 전달률은 100%, 단순 평균은 50%였다.
움직이는 경계의 현재 RAW 대비 추가 MAE는 잡음 OFF에서 단순 평균 48.175 DN,
새 필터 0 DN이다. 이는 원래 위상의 평가이며 네 위상의 사전 기준 모두 통과했다.
추가 MAE는 **현재 취득 대비 처리로 추가한 오차**이고, 실제 장면 정답 대비 전체
카메라 오차가 아니다. 평가 영역만 post-optics 광전하 변화로 정의했고 이 배열은
필터 입력으로 사용하지 않았다.

잡음 ON에서 정적 후반 변화폭 범위는 무처리 0.7795–5.7689%, 새 필터
0.3400–1.4278%였다. 움직이는 경계의 추가 MAE는 새 필터 0.706–1.060 DN,
단순 평균 48.210–48.667 DN이었다. 임계값은 결과 생성 전에 study-plan에 고정했다.

**보편 적용은 부적절하다.** 일정 광원 아래 진짜 장면 밝기를 0.98/1.02로
교대시킨 반례에서 실제 변화폭 3.79117%를 새 필터도 0%로 지웠다.
작은 실제 주기 변화와 카메라 유래 주기 변동이 RAW에서 구분되지 않는 경우가 있다.
따라서 unknown scene의 인지 입력을 이 출력으로 대체하지 않는다. 원본 경로를
보존하고, 알려진 시간 변동을 평가하는 선택 가능한 연구 후보로만 사용한다.

[결과표](../reports/camera_platform/pwm-fixed-rate-20260926/report.md) ·
[직접 열 수 있는 영상 비교](../reports/camera_platform/pwm-fixed-rate-20260926/comparison.html) ·
[수치 결과](../reports/camera_platform/pwm-fixed-rate-20260926/results.json)

실행: 프로젝트 루트에서 아래 명령으로 새 결과 폴더를 지정한다. 기존 결과를 덮어쓰지 않는다.

```sh
PYTHONDONTWRITEBYTECODE=1 .venv-platform/bin/python -B tools/run_camera_pwm_fixed_rate.py \
  --output reports/camera_platform/pwm-fixed-rate-new-study
```

핵심 시험 10개가 통과했다. 기존 설치/Worker/브라우저 재시작 시험은 반복하지 않았다.
기본 앱·DB·등록 모델은 그대로이며 실제 센서, 다주파 PWM, 일반 3D 운동과 인지
성능, 실제 해상도/실시간 처리율은 이번 결과로 입증하지 않는다.
