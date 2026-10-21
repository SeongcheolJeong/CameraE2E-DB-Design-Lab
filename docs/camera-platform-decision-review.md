# 저장된 설계 결정 비교 작업실

기존 신호 우선 선택과 움직임·RAW 채널비 유지 선택을 하나의 읽기 전용 화면에
연결했다. 계산 엔진이나 선택 함수를 다시 실행하지 않으며, 원본 결정·모델을
수정하지 않는다. 전체 범용 실험 관리기를 새로 만드는 단계가 아니라, 현재 완료한
두 결정과 그 근거를 실제로 확인할 수 있게 하는 개발 소스 기능이다.

## 연결한 두 결정

| 저장 연구 | 조건부 선택 | 평가 범위 |
|---|---|---|
| `constrained-selection-20260926` | radial·4 ms·gain 1 | 신호·예측 SNR·well/RAW 포화·노출 상한 |
| `motion-color-tradeoff-20260926` | clear·2 ms·gain 1 | 기존 제약 + signed motion transfer·고정 보정 후 채널비 |

첫 연구의 선택용/평가 121개 결과와 두 번째 연구의 개발/평가 18개 결과를 표시한다.
두 번째 연구의 neutral·flat·정적 기준을 포함한 전체 취득 수는 40이며, 화면에
나열되는 18개를 전체 취득 수로 재집계하지 않는다. 이번 연결의 새 카메라 취득은 0건이다.

## 실행

```sh
cd /Users/seongcheoljeong/Documents/CameraE2E
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  .venv-platform/bin/python tools/open_camera_decision_review.py --serve
```

주소: `http://127.0.0.1:8797/decision-review.html`.
이 서버는 registry/DB를 열지 않고 Worker를 시작하지 않는다. GET/HEAD 외 요청과
외부 Host/Origin을 거부한다. 종료는 실행 터미널에서 Ctrl-C다.
기존 기본 앱 8770과 설치 데이터는 변경하지 않는다.

개발 소스의 기존 앱에서도 **실험 · 비교 → 두 결정과 적용 한계 보기**로 진입한다.
기본 설치 앱 v0.15에 이 버튼이 배포된 것은 아니다. 별도 읽기 전용 서버의 상단
돌아가기 링크는 기존 8770 작업실을 가리킨다.

## 사용 흐름

1. 두 카드에서 결정 기록을 고른다. 후보 재선택이나 최적화 실행 버튼이 아니다.
2. 당시 제약과 고정 시각, 선택용·미사용 평가·스트레스·잡음 ON/OFF 집계를 확인한다.
3. 평가 구분과 입력을 좁힌다. 실패 필터는 동일 입력의 통과 후보도 함께 남긴다.
4. **같은 입력 RAW**를 누르면 같은 위상/장면/seed의 후보 원본을 함께 연다.
5. 원본 NPY 또는 동결된 계획·선택·결과·보정 JSON을 내려받는다.

`?profile=motion&split=stress` 등 URL로 선택한 결정/평가 구분을 다시 열 수 있다.
원본 이미지는 실제 native 16×16 정수 코드의 회색조 미리보기이며 모든 후보에
동일한 64–4095 DN 표시 범위를 적용한다. 다운로드 NPY는 NPZ에 저장된 원본
`raw_dn.npy` 멤버 바이트 그대로다. 화면용 표시만 8-bit PNG로 바꾼다.

## 수치와 근거 의미

- SNR은 모델 예측의 Green p10 선형비이다. 실측 잡음, dB, 전체 영상 평균과 다르다.
- 광전하 대비와 정수 RAW 대비를 별도 열로 보여주고 음수 대비 반전을 보존한다.
- clear·2 ms가 채널비 기준이므로 해당 후보의 잔차 0은 정의상 결과다. 절대 색 정확도나 ΔE가 아니다.
- 최종 코드 4095의 마스크는 **RAW 상단 마스크**다. well 포화 마스크라고 표시하지 않는다.
  Well 포화율은 저장된 결과의 수치로 따로 표시한다.
- 움직임 연구는 공통 중심시각 global box shutter, 500/600 nm, 잡음 OFF 조건이다.
  rolling shutter·트랩·일반 3D·실물 검증 결과로 확대하지 않는다.
- 두 후보가 첫 신호 기준에서 적합했다는 사실과 새 움직임 기준의 적합성을 API에서도
  `prior_signal_feasible`, `motion_development_feasible`로 구분한다.

## 구현과 읽기 경계

`decision_review.py`는 두 연구의 plan/results/choice/calibration SHA256을 명시적으로
고정한다. 선택과 결과의 연결을 확인하되 후보 선택을 다시 계산하지 않는다.
목록·수치표는 JSON만 읽고, 선택한 사례에서만 NPZ의 원본 RAW 멤버를 읽는다.
저장된 RAW fingerprint와 불일치하면 미리보기와 다운로드를 거부한다.
아카이브 파일 이름은 저장된 결과에서만 선택하며 요청자가 경로를 입력할 수 없다.
이 읽기 경계는 NPZ 전체의 물리 정확도 검증이나 새 광학 검증이 아니다.

기존 앱의 구체적인 GET 경로는 범용 registry 경로보다 먼저 등록한다.
새 collection/job schema나 write endpoint는 추가하지 않았다.

```text
GET /api/v1/decision-review
GET /api/v1/decision-review/{signal|motion}/cases/{case_id}
GET /api/v1/decision-review/{signal|motion}/cases/{case_id}/raw
GET /api/v1/decision-review/{signal|motion}/documents/{results|plan|choice|calibration}
```

## 이번 확인

새 관련 시험 10개: 파일 변조, RAW 변조, 읽기 전용 경계, 조회 중 RAW 미로딩,
구체 API 경로, 음수 대비·색 기준 의미·평가 분리·원본 NPY 유지.
실제 Chromium 확인 15항목: 두 선택 전환, 실패 필터, 동일 입력 RAW/마스크,
NPY/JSON 다운로드, deep link, 390px 모바일 폭, 페이지 write 요청 0건.
별도 카메라/Factory 계산, Worker·설치·전체 앱 회귀는 반복하지 않았다.

근거: `reports/camera_platform/decision-review-20260926/`.
