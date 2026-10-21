# 전방 보행자 안정 인지와 제동 기회 연구

현재 구현은 nuScenes 전방 시퀀스 → RGB 분광 타일 → 기존 DB Lens×Sensor의
native 평균 광전자·정수 RAW → 고정 ISP → 고정 YOLO11n → 인과적 track →
사건별 인지 시점·제동 여유를 연결한다. 정식 위험 사건과 충분한 저장 공간을
확보하기 전에는 개선이나 설계 추천이 완료된 것으로 표시하지 않는다.

## 현재 실행 근거와 남은 입력

- 공개 mini에서 CAM_FRONT 이미지와 메타데이터만 확보했다. 약 440MiB를 저장했다.
- `outputs/pedestrian-braking/input-inventory-mini.json`: 현재 조건의 위험 사건 0개,
  비위험 후보 구간 12개. 구간 수와 독립 로그 수는 다르므로 12개 평가 코호트가 아니다.
- `outputs/pedestrian-braking/original-sequence-diagnostic/diagnostic.json`: 원본 RGB의
  고정 30° 관측창을 1280×720에서 실제 YOLO로 72프레임 추론했다. 38개 검출을 보존했다.
  카메라 시뮬레이션·저광량 재촬영·제동 개선의 결과가 아니다.
- `outputs/pedestrian-braking/full-window-first-frame/connection-result.json`: 실제
  도로 한 프레임에 L1-S1·L2-S1을 계산했다. 정수 RAW는 각각 1862×3296,
  2986×5288이며 공통 고정 ISP 출력은 1280×720이다. 각 5개 seed 모두
  보행자 검출 0개였다. 원본·평균 광전자·RAW·ISP·모델·추론 기록의 manifest를
  `first-frame-verification.json`에서 확인했다. 시간 인지·제동 성과·샘플링 수렴의
  검증은 아니며 추천 후보를 만들지 않았다.
- 첫 프레임의 광학 평균 계산은 L1-S1 약 11.7분, L2-S1 약 25분 걸렸다.
  차량 처리 지연으로 사용하지 않는다. 충분한 저장 공간만으로 본 평가가 빠르게
  끝나는 것은 아니며, 실행 효율도 후속 개선 항목이다.
- `outputs/pedestrian-braking/resource-estimate.json`: 12구간·5개 seed의 보수적 예상
  출력량은 약 1.96TB다. 원본 해상도·사건 수를 자동으로 낮추지 않는다.
- [공식 AWS 공개 배포](https://registry.opendata.aws/motional-nuscenes/)에서 정식 trainval
  메타데이터를 확보했다. 입력 사진 전체 대신 메타데이터로 먼저 적격성을 판단했다.
- `outputs/pedestrian-braking/input-inventory-trainval.json`: 현재 위험 사건 0개,
  비위험 후보 구간 1301개. 402개 보행자/구간은 기록된 6초 경로에 충돌 경계가
  없어 제외됐고, 1506개는 이동 속도 조건, 827개는 보이는 주석 수 조건에서 제외됐다.
  이 숫자는 독립 사건 수가 아니라 보행자/구간별 제외 횟수다.
- 중요한 현재 한계: `collision_path_coordinate`는 기록 경로 밖을 연장하지 않는다.
  따라서 실제 경로가 충돌 경계까지 도달하지 않은 정상 주행에서는 거리를 정의하지
  못한다. 제동 기회를 평가하려면 직선 충돌 경계 투영 가정을 명시하거나, 현재
  계약에 맞는 별도 시퀀스가 필요하다. 조건을 몰래 완화해 코호트를 만들지 않는다.
- 충분한 출력 볼륨도 필요하다. 입력 적격성이 확정되기 전에는 수십 GB의 전체
  카메라 아카이브를 추가로 내려받지 않는다.

## 재실행

프로젝트의 `.venv-platform`과 기존 오프라인 DB·display 자산을 사용한다.
인지 모델은 기존 Python 3.9 오프라인 환경의 로컬 YOLO11n을 사용한다.
모델 파일과 환경은 변경·자동 다운로드하지 않는다. 모델 실행에는 기존
`CAMERA_PLATFORM_TRUSTED_YOLO_WEIGHTS_SHA256` 선언이 필요하며, 이는 파일의
출처 인증이나 모델 훈련 데이터의 비중복 증명은 아니다.

```sh
PYTHONPATH=src .venv-platform/bin/python -B tools/run_pedestrian_braking_study.py inventory --dataset-root <nuScenes root> --version v1.0-trainval --output <inventory.json>
PYTHONPATH=src .venv-platform/bin/python -B tools/run_pedestrian_braking_study.py freeze --dataset-root <nuScenes root> --version v1.0-trainval --package <new package>
PYTHONPATH=src .venv-platform/bin/python -B tools/run_pedestrian_braking_study.py preflight --package <package>
PYTHONPATH=src .venv-platform/bin/python -B tools/run_pedestrian_braking_study.py first-sequence --package <package>
PYTHONPATH=src .venv-platform/bin/python -B tools/run_pedestrian_braking_study.py run --package <package>
PYTHONPATH=src .venv-platform/bin/python -B tools/run_pedestrian_braking_study.py verify --package <package>
```

`replay --package <completed> --destination <new directory>`는 같은 명세와 사진
snapshot을 복사하고 평균·RAW·인지 결과를 새로 계산한다. 완료 package는 덮어쓰지 않는다.
첫 프레임 연결만 확인하려면 `first-frame-connection --dataset-root <mini root>
--output <new diagnostic directory>`를 사용한다. 위험 코호트·시간 성과로 승격되지 않는다.

## 계산과 평가 계약

이전 네 가상 조합을 유지한다. 입력·검출 관측창은 공통 30°이며 PSF·픽셀·ISP
주변 지지를 확보하는 가상 센서 extent는 32°로 명시한다. 면적·native 픽셀 수와
여기에 따른 비용을 공개한다. 실제 센서 제품의 active-array에 맞는 조합은 아니다.

노출 4ms·gain 1·추가 광량 0.25배, 기존 LCD-Apple sparse spectral proxy와
센서별 고정 색 변환을 사용한다. 후보별 자동 밝기·문턱값 조정을 하지 않는다.
native 격자에서 적분·취득·ISP를 계산한 뒤 공통 1280×720으로 이동한다.
PSF·픽셀·demosaic·anti-alias halo를 포함하고 전체 분광 cube는 만들지 않는다.
전체 native 평균 및 취득 배열은 RAM에 보유하므로 광학 타일 정책을 전체 RSS
상한으로 해석하면 안 된다. preflight에는 native 취득·ISP 작업량 추정도 표시한다.

대표 3프레임에서 입력 격자 refinement 1→2를 비교한다. 평균 전자와 잡음 OFF
윤곽 gradient 차이 1% 이하, 검출 개수·3프레임 안정 상태 동일, 대응 box IoU ≥0.9,
score 차이 ≤0.01일 때 해당 격자를 채택한다. 불안정하면 2→4를 비교하고,
통과한 coarse 격자로 실제 시퀀스를 계산한다. 끝까지 불안정하면 선택을 막는다.
저장 기하 PSF 자체의 refinement·회절·CRA 검증과는 별개다.

YOLO11n은 person, confidence 0.25, NMS IoU 0.7, imgsz 1280으로 고정한다.
시퀀스마다 모델을 한 번 로드하며 tracker는 관측만 받아 일정 속도 예측과 IoU
0.3 일대일 연관을 수행한다. 미래 주석·객체 위치는 추적기에 전달하지 않는다.
실제 검출 3회 연속일 때 안정 상태가 된다. 누락 2회 또는 시간 gap >0.125s이면
연속성을 끊고, 예측 box만으로 안정 상태를 유지하지 않는다.

위험 사건은 6초의 직선 구간(heading 범위 5° 이내), 보행자 최대 속도 ≤0.5m/s,
자차 속도 ≥2m/s, 기록 경로에서 제동 기준선을 통과하는 조건으로 고른다.
차량 폭 2m·ego pose에서 전면 2.5m, 보행자 footprint의 외접원은 시험용 기하
가정이다. 남은 거리는 기록 polyline의 arc length를 따라 전면 충돌 경계까지
계산하며 카메라–보행자 직선거리로 대체하지 않는다. 선정 4위험+2비위험,
평가 4위험+2비위험은 12개 서로 다른 로그에서 배정한다.

원본 3D 박스의 투영은 검토된 2D silhouette가 아니다. 평가에서만 2Hz 주석
anchor의 IoU 0.5와 instance ID를 사용한다. 연관이 양 끝에서 확인된 구간을
시간·거리 범위로 평가하고, 끝까지 놓친 사건과 불확실한 사건도 분모에 넣는다.
카메라 주석의 완전성이 미검토 상태면 미연관 track은 확정 오검출과 구분한다.
앞에서 정답과 일치한 track도 주석 범위 밖의 꼬리나 이후 ID 불일치까지 확인된
것으로 처리하지 않는다. 해당 구간의 오검출 가능성을 상한에 남긴다.
후보 오검출 상한이 기준안 확정 오검출 수를 넘으면 비증가를 인정하지 않는다.

시각은 세 번째 관측 결과 사용 가능 시각이며 처리 지연 공통 100ms를 포함한다.
50/100/200ms 민감도와 기록 구간에서 허용 가능한 최대 지연(구간 종료 시 censored)을
보고한다. 이 값은 차량 지연 실측이 아니다. 정지거리는 기록 속도 v로
`v×0.3 + v²/(2×6) + 2m`를 적용한다. 50km/h를 기록 시간축에 억지로 대입하지 않는다.

각 사건의 다섯 취득 seed는 반복이지 독립 사건이 아니다. 모든 반복에서 성공
전환한 사건이 늘고, 기존 성공 사건의 퇴행·안정 오검출 증가가 없어야 추천한다.
선정을 hash로 고정한 후 평가용에서는 기준·선택만 계산하며 재선정하지 않는다.

## API·작업실과 출력

`/pedestrian-braking.html`에서 고정 package 경로와 명세 SHA로 제출·재개·취소한다.
`GET/POST /api/v1/pedestrian-braking-studies`, `/{id}`, `/{id}/cancel`,
`/{id}/files/{relative}`와 `GET .../template`를 제공한다.
SDK의 `submit_pedestrian_braking_study`와 `wait_pedestrian_braking_study`는
공통 `/api/v1/jobs/{id}?expected_kind=pedestrian_braking_study`를 사용한다.

외부 볼륨은 운영자가 `CAMERA_PLATFORM_PEDESTRIAN_PACKAGE_ROOTS`에 명시한
루트 아래에서만 API에 허용한다. CLI는 직접 지정한 경로를 사용한다.
명세·RAW·ISP·인지 파일의 변경, 다른 sampling 결과의 캐시와 완료 manifest
변경을 거부한다. `.execution.lock`으로 동일 package의 동시 수정을 막는다.

보고서는 동기 영상·검출·track·인지 결과 사용 시각, 모든 사건/seed의 성공·놓침·
불확실성·제동 여유·오검출과 비용을 표시한다. RAW와 선형 ISP, 입력 원본,
모델 revision, 가중치·worker·RGB 해시, selection lock을 함께 보존한다.
이 결과는 RGB 근사 입력의 개방 루프 연구이며 실제 충돌 회피율이나 AEB 인증이 아니다.
작업실의 `/api/v1/pedestrian-braking-studies/intake`는 작은 입력 점검 요약을 표시한다.
첫 프레임 진단은 완료 manifest에 묶인 파일만 별도 route로 제공하며, 정식 study
성공이나 추천 결과로 등록하지 않는다. 첫 시퀀스와 전체 평가의 review manifest도
분리해, 다른 단계에서 바뀐 결과를 이전 결과로 표시하지 않는다.
