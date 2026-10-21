# 설치된 기본 작업실의 처방 광학 예제 검증

`tools/verify_camera_prescription_delivery.py`는 설치 전환이 끝난 기본 작업실에 명시적인
합성 예제를 추가하고 실제 API/Worker 결과를 확인하는 전용 도구입니다. 기존 QA 검증기의
기본 작업실 거부 조건은 변경하지 않으며, 그 검증 함수를 우회하거나 호출하지 않습니다.
이 도구는 서버를 시작·정지하거나 설치 선택을 변경하지 않습니다.

## 실행 조건

기본 상태 경로, 설치 Runtime, manifest SHA256, 제품 버전, 기존 행 baseline의 SHA256,
예제의 출발 Assembly ID를 모두 명시해야 합니다. 실제 설치 프로세스·정적 파일·버전과
클라이언트 import 경계를 확인하며 각 POST 직전에도 재확인합니다. 기존 baseline의 모든
행을 보존하고 첫 실행 시점의 추가 기존 행도 유휴 상태와 같은 SQLite snapshot으로
고정합니다. 이 snapshot은 새 기록 추가를 허용하지만 기존 행 변경·삭제는 거부합니다.

다음은 준비된 v0.14 후보의 명시적 입력입니다. 먼저 전체 설치 전환 검증과 전환을
완료해야 합니다. 기본 작업실이 v0.13인 동안에는 버전·pin 검사에서 거부됩니다.

```sh
.venv-platform/bin/python -I -B tools/verify_camera_prescription_delivery.py \
  --base-url http://127.0.0.1:8770 \
  --expected-state /Users/seongcheoljeong/Documents/CameraE2E/outputs/camera-platform \
  --expected-runtime-root /Users/seongcheoljeong/Documents/CameraE2E/outputs/camera-platform-runtimes/7d3f5fa4e5afcdbfe160-3a21cbae7320c974 \
  --expected-manifest-sha256 e9d04597f828c0d45ceb7b5261c204a813afea4091e7e68b6dc358ef455e0f16 \
  --expected-version 0.14.0 \
  --baseline reports/camera_platform/prescription-v014-primary-baseline.json \
  --baseline-sha256 a687530d87b7b5bbd7ee3c463c6e2e21990bc242e9c85c6d30e6b783b301a676 \
  --source-assembly-id assemblie-3611a084401040c3850d1fe88dab5190 \
  --output reports/camera_platform/prescription-primary-delivery.json
```

기본 모드는 GET·파일 확인만 하는 사전검사입니다. 같은 명령에 `--apply`를 추가하면
새 예제의 Factory 2건, 모델·Assembly·분광 차트·Test·Run·Replay를 만듭니다. 정상 최초
진행은 11개 POST이며 기존 객체를 수정하지 않습니다. 출발 Assembly와 Sensor/ISP는
기존 선언된 공학 fixture를 참조하고 새로운 모델과 Assembly를 별도 ID로 등록합니다.

`--apply` 재실행 시 **같은 output과 모든 입력을 유지**해야 합니다. 제출 의도를 먼저
디스크에 쓰고 반환 ID를 즉시 저장합니다. 응답이 끊겼다면 저장된 요청과 일치하는 객체가
정확히 1개인 경우에만 연결합니다. 0개 또는 여러 개라면 중단하며 POST를 반복하지 않습니다.
관측 timeout은 실행 중 작업을 취소하지 않습니다. 보고서의 ID와 마지막 작업 상태를
확인하고 같은 명령으로 다시 관측합니다. 새 output을 만들어 불확실한 제출을 회피하지 마세요.

완료 뒤 같은 명령에 `--read-only`를 추가하면 저장된 ID를 다시 검증합니다. 이 모드는
POST를 금지하고 전체 전후 행 snapshot도 같아야 합니다. `--apply`와 동시에 사용할 수
없습니다. 보고서 경로는 baseline·기본 state·설치 Runtime 외부의 일반 파일이어야 하며
심볼릭 링크나 `..`를 이용한 경로 우회도 거부합니다.

## 계산과 근거 범위

고정 처방의 RayOptics 광량·입사각 응답과 위치별 PSF를 실제 Worker가 계산합니다. 두 결과를
같은 처방의 부모 모델로 묶고, 3 µm·128×128 Sensor의 중앙 64×64 분광 차트에서 RAW/RGB
2프레임을 생성합니다. ISP white balance를 바꾼 재처리에서는 RAW가 그대로이고 RGB가
달라야 합니다. 각 원본의 SHA256·바이트 수, 카메라/재처리 ZIP의 정확한 모델·Factory
계보와 모든 원본 artifact를 확인합니다.

ZIP 자체는 생성 시각에 따라 달라질 수 있으므로 다운로드 SHA256은 호출별 관측으로
남깁니다. 내용의 package hash와 계보·원본 artifact는 불변 근거로 비교합니다. 모든 결과는
합성 데이터이며 물리 검증은 `not_established`, 총 불확실도는 `unknown`으로 유지합니다.
이 예제는 독립 실측·고객 OS 설치·화면 검증을 대신하지 않습니다.

## 도구 검증

`tests/platform/test_prescription_delivery.py`의 32개 시험과 Ruff를 통과했습니다. 잘못된
state·pin·버전·Worker·baseline 거부, 기존 행 보존, 응답 유실·중복 후보·변경된 요청의
재제출 방지, read-only 재검증, ZIP timestamp·중복 경로 검사 등을 포함합니다.

또한 임시 TestClient에서 실제 Factory·영상·재처리 계산과 ZIP을 끝까지 실행했고,
11개 POST 이후 저장된 ID로 재검증할 때 추가 POST가 0개이며 기존 모든 행이 같음을
확인했습니다. 이 시험은 임시 state에서 서비스 계산을 직접 진행한 것으로, 기본 작업실의
분리된 Worker 실행 증거는 아닙니다. 실제 기본 작업실 검증은 위 설치 전환 이후의
별도 실행 보고서가 필요합니다.

## v0.14 기본 작업실 실행 결과

2026-09-20 [기본 작업실 실행 보고서](../reports/camera_platform/prescription-primary-delivery.json)는
실제 분리 Worker의 위 계산을 완료했습니다. 최초 POST 11개 뒤 동일 저널의 읽기 전용
재검증에서 POST 0개와 전체 행 불변을 확인했습니다. [이전 시간 촬영 재현](../reports/camera_platform/primary-prescription-legacy-rerun.json),
[무결성](../reports/camera_platform/primary-prescription-integrity.json),
[기본 화면 조회](../reports/camera_platform/prescription-ui/primary-v014/verification.json)를 함께 확인합니다.
이는 위 임시 TestClient 시험과 별도의 실행 근거이며 독립 실측을 뜻하지 않습니다.
