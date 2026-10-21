# 도로 HDR: native 취득, 재합성, 인지·시간 평가

2026-10-04 개발 연결. 결과 제목은 **RGB 기반 근사 도로 장면에서 HDR이 보행자 안정 인지 시점과 제동 판단 여유에 미치는 영향**이다.

## 실행

```sh
PYTHONPATH=src .venv-platform/bin/python tools/run_road_hdr_study.py freeze-first --package outputs/new-road-hdr
PYTHONPATH=src .venv-platform/bin/python tools/run_road_hdr_study.py first-photo --package outputs/new-road-hdr
PYTHONPATH=src .venv-platform/bin/python tools/run_road_hdr_study.py reprocess --package outputs/new-road-hdr
```

새 작업실 `/road-hdr.html`에서도 고정·제출·조회·취소·재합성이 가능하다. 이미 고정된 패키지는 specification-binding.json의 SHA256을 입력한다. 첫 사진은 1 seed의 연결 시험이며 최종 사건 평가가 아니다.

시퀀스 입력은 완전한 로컬 nuScenes 데이터를 요구한다. 기존 사진의 정답·검출 결과로 광량 조건이나 입력 분할을 조정하지 않는다.

```sh
PYTHONPATH=src .venv-platform/bin/python tools/run_road_hdr_study.py freeze-sequences \
  --dataset-root /path/to/authorized/nuscenes --version v1.0-trainval \
  --package /Volumes/Research/road-hdr-study
PYTHONPATH=src .venv-platform/bin/python tools/run_road_hdr_study.py preflight \
  --package /Volumes/Research/road-hdr-study --stage full
PYTHONPATH=src .venv-platform/bin/python tools/run_road_hdr_study.py evaluate \
  --package /Volumes/Research/road-hdr-study
```

API 접근을 허용할 외부 루트는 서버 실행 시 `CAMERA_PLATFORM_ROAD_HDR_PACKAGE_ROOTS`에 지정한다. 파일 경로에 해당 볼륨 이름을 쓰는 것만으로 충분하지 않다. 실제로 별도 장치가 연결되고 저장 용량 검사에 통과해야 한다.

## 공통 SDK

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8771") as client:
    frozen = client.freeze_road_hdr("outputs/new-road-hdr")
    request = {"schema": "camera_road_hdr_job_v1", "name": "도로 HDR",
               **frozen, "action": "first-photo"}
    captured = client.submit_road_hdr_acquisition(request)
    client.wait(captured["id"], expected_kind="road_hdr_study")
    replay = client.submit_road_hdr_reprocessing(request)
    result = client.wait_road_hdr(replay["id"])
    print(result["result"]["summary"]["viewer_path"])
```

`/api/v1/road-hdr-studies`의 action은 `first-photo`, `acquire`, `reprocess`, `evaluate`이다. acquire는 전체 사건용이고 reprocess는 보존된 RAW만 읽는다. `/api/v1/jobs/{id}` 기반 공통 SDK wait가 동일하게 동작한다. 실행 중 동일 패키지의 두 작업은 허용하지 않는다.

## 수치·시간 계약

- L1-S1 DB revision, pitch, 32° 주변 입력, 30° 전방 관측창을 유지한다. 640×360은 공통 출력 격자이며 native RAW는 약 1874×3304이다. 가상 sensor extent이다.
- RGB의 display gamma·primary 스펙트럼으로 분광 proxy를 만든다. 원본 촬영 흔적은 제거하지 않는다. 0.25배·1배와 원본 선형 RGB 휘도 >0.8인 위치의 16배 광량 스트레스 조건을 계산 전에 고정한다.
- 기존 CameraE2E 광학 타일·픽셀 적분과 정수 readout을 사용한다. 평균 전하만 선형 재사용한다. 노출별 shot/read RNG는 독립이고 FPN 지도는 공유한다.
- 저장 모델의 알려진 FPN 계수로 모든 비교군을 같은 방식으로 보정한다. 이는 **모델 계수의 완전한 지식을 가정한 보정**이며 실측 calibration이 아니다.
- 기존 HDR의 정적 검사와 크기 제한은 유지한다. 새 adapter는 native 타일에서 FPN 역변환을 재사용한다. 정적 진단만 유효한 긴 노출로 짧은 노출의 무효 영역을 채울 수 있다. global/local 기준 프레임 보존에서는 채우지 않는다.
- global은 기존 RAW 짝수 정수 이동 점수 함수를 16개 분산 native 창에서 실행한 제한적 adapter이다. 기존 전역 전체 배열 결과와 동일한 계산량·정밀도를 주장하지 않는다. local은 RAW guide의 subpixel 이동, local은 phase correlation 초기화와 양방향 Farneback을 사용한다. guide 한 변은 최대 640이며 각 CFA site는 별도로 bilinear warp한다. 64 native pixel을 넘는 이동, 양방향 guide 오차 >0.75, 구조 손실, 5σ 잔차 및 주변 3×3 guard는 긴 기여를 배제한다.
- 조건부 대각 분산은 `sum(w² var)`로 전달한다. 보간이 만든 공간 covariance는 잔차 검사에 상한으로 반영하지만 완전한 posterior나 물리 uncertainty는 아니다. flow·blur 신뢰도를 완전히 검증한 알고리즘으로 부르지 않는다.
- 모든 비교군은 같은 전자율 demosaic/WB/CCM/tone을 쓴다. 무효 지원 영역은 미리보기에서 검게 표시하고 valid mask를 별도 보존한다. 잡음 guard가 많은 검은 영역을 만들면 그것을 물리적인 카메라 암부 자체로 해석하지 않는다.
- 실제 source timestamp에서 긴/짧은 노출을 번갈아 취득하고 최신 짧은 시점에 과거 완료된 긴 노출만 사용한다. 단일 노출은 모든 source timestamp에서 출력한다. 첫 정적 사진의 브래킷은 인과적 시퀀스 취득의 증거가 아니다.
- readout 5ms, 공통 후처리 100ms, HDR 추가 15ms는 가정이다. 추가 HDR 0/5/15/30/60ms를 평가한다. simulator wall clock은 별도로 기록한다.
- tracker는 해당 프로그램의 예정 출력 시각으로 누락을 판단한다. 실제 관측 3회가 필요하고 세 번째 결과의 사용 시각으로 판단한다. 예측 유지·미래 GT로 앞당기지 않는다.

## 사건과 판정

4 위험 + 2 비위험 사건을 각 분할에 두며 12개 로그는 서로 달라야 한다. 이동하는 전방 보행자도 허용한다. 직선 진행 corridor에서의 시각별 정답 위치로 제동 경계를 투영하며, corridor 진입·이탈이나 주석 범위 밖은 제동 판정의 미결정 사유로 남긴다. 사건 전체가 계속 창 안에 있거나 기록 중 제동 기준선을 반드시 넘어야 한다고 요구하지 않는다. 이는 미래 횡단·충돌 예측 모델이 아니다.

2Hz 주석은 해당 출력 track이 앞뒤 정답 box 모두와 겹칠 때만 연관을 인정하고 인지 시점을 구간으로 보고한다. GT는 평가에만 쓰인다. 불완전 주석은 false track의 가능한 상한으로 남는다.

선정용에서 단일 1/4/8ms 중 기준안을 고정한 뒤 global/local, 긴 4/8ms HDR을 비교한다. 선택 lock의 명세·기록 해시가 바뀌면 held-out 재실행을 거부한다. 최소 한 사건의 5 seed 모두에서 성공 전환, 기존 성공 사건 퇴행 없음, 오검출 상한 증가 배제 없이는 개선 권장을 하지 않는다. 반복 프레임·seed는 사건 수가 아니다.

전체 광학 입력/PSF 수렴은 이 기능의 단위 검사로 확립되지 않는다. 별도 sampling qualification이 없는 결과는 최종 설계 권장에 사용하지 않으며 연결 진단으로 남긴다. 모델의 기하 PSF·QE proxy·가정 readout, 데이터 권한, 처리 지연·주석의 한계를 인도 보고서에 유지한다.

현재 외부 저장소와 12개 적격 사건이 없으면 전체 평가가 차단된다. 첫 사진과 API/수치 테스트를 전체 도로 인지·제동 개선 증거로 확대하지 않는다.
