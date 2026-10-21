# v0.16 설치 후보와 검증 범위

2026-09-23 기준 기본 작업실은 v0.15다. 이 문서는 **별도 v0.16 설치 후보**의
근거와 아직 남은 전환 조건을 설명한다. 원래의 7개 모듈·4개 실행 방식과
일반 광학·픽셀·전기 모델, HDR/LFM, ISP·최적화 우선순위를 유지한다.

## 독립 실행 구성

후보 Wheel의 SHA-256은
`14440042e3cc85faba7a2f90e603692b92aa82a9df7a6485771e40d875a69817`이다.
제품 파일 748개를 고정했고, 검증된 dev16 제품에서 버전 표기만 0.16.0으로 바꿨다.
새 환경에는 제품을 포함해 92개 배포 아티팩트를 설치했으며 `pip check`를 통과했다.
NumPy 2.5.3·SciPy 1.18.1을 포함하는 의존성 환경의 해시는
`37e658353c62ccd83e41da0851dcf14a5343e584583c18249060bb2d721665f4`다.

`installed-runtime.json`의 v2 형식은 패키지뿐 아니라 Python 실행 경로·실행 파일과
가상 환경 신원, 자산 Manifest를 고정한다. 런처와 재시작은 이 선택을 검증한다.
Python 경로를 실제 바이너리 경로로 치환하면 가상 환경을 잃을 수 있으므로 원래
가상 환경 경로를 유지한다. 자산 2,430개 파일은 별도 관리 디렉터리에 저장하고
서명된 것으로 표현하지 않는 고정 SHA-256 검증으로 출처와 바이트를 확인한다.
이는 배포권 확인을 대신하지 않는다.

Meep와 Mitsuba는 기존 별도 환경을 사용한다. Python 자체의 휴대용 배포,
외부 Solver 설치, 모든 빌드 의존성의 오프라인 재현, 고객 OS 검증은 남아 있다.

## 현재 확인한 동작

- 비구면 처방 → OPD·PSF → 실제 RAW/RGB. 같은 시험의 구면 비교와 결과 차이를 확인했다.
- 잔류 전하를 유지하는 native 픽셀 촬영, 선형·비선형 readout, HCG/LCG 경로.
- 시간 노출과 PWM 위상에 따른 HDR 입력 차이 및 지원하지 않는 합성의 거부.
- 보존된 RAW의 공간·인과적 시간 ISP 재처리와 후보 비교. UI에서 새 모델을 등록하고
  재처리와 6개 후보 비교를 실행한 뒤 독립 계산과 원본 번들을 대조했다.
- ISP 탐색 18개 tuning 조건과 별도 6개 validation 조건의 실행.

[설치 실행](../reports/camera_platform/install-v016/installed-runtime-independent/verification.json),
[도메인 실행](../reports/camera_platform/install-v016/domain-http/campaign-summary.json),
[선형 DCG 보충](../reports/camera_platform/install-v016/nonlinear-dcg-supplement/http/http-verification.json),
[실제 UI 제출](../reports/camera_platform/install-v016/domain-ui-submission-journal.json)을 별도 기록한다.
HTTP 검증의 수천 개 확인 항목에는 반복되는 원본·해시 검사가 포함되며 독립 시험 수나
상용화 완료율로 사용하지 않는다.

최초 전체 Python 회귀는 4,161개 통과·5개 실패였다. 실패 원인은 환경 버전 표기와
두 부동소수점 중간 결과의 FFT 반올림 차이였다. RAW/RGB는 정확히 같았다.
같은 환경에서는 과거 dev7과 현재 결과가 모두 같으며 독립 직접 합산도 확인했다.
버전별 계약을 명시한 두 시험 파일은 두 환경에서 각각 88개 전체 통과했다.
[조정 기록](../reports/camera_platform/install-v016/regression-diagnosis/reconciliation.json)은
원래 4,166개 시험의 실패 해소를 뜻하며, 수정 후 전체 재실행으로 표현하지 않는다.

## 설치 전환과 복구

v0.16은 Registry schema 1을 2로 변경한다. 기존의 코드 선택만 되돌리는 방법은
이전 버전의 초기화가 schema metadata를 손상시킬 수 있어 사용할 수 없다.
새 복구 도구는 상태별 전환 잠금, 프로세스 신원·하위 프로세스 종료 확인,
WAL을 포함하는 DB 증거, 모든 Blob 바이트를 확인한다.

새 작업이 없고 정확히 알려진 파생 인덱스 추가만 존재할 때만 트랜잭션 안에서
schema를 역변환한다. 신규 기록·작업·Blob이 생겼거나 상태가 불명확하면 후보 데이터와
선택을 유지하고 `forward_recovery_required`로 남긴다. 기존 백업 ZIP으로 상태를
덮어쓰지 않는다. 동작 시험과 실제 프로세스 복구는 별도 근거로 남긴다.

전체 전환에는 기존 장면 작성·카메라 준비·광휘 재사용·처방 UI 조건도 필요하다.
새 광학·전기·ISP 검증으로 이 조건을 대체하지 않는다. 최신 진행 상태는
[작업 상태](../reports/camera_platform/install-v016/work-status.json)를 따른다.

최종 복구 도구 관련 296개 시험과 독립 검토를 통과했다. 실제로 별도 상태 두 개에서
새 작업 없는 schema 역변환 후 v0.15 재실행, 새 작업 있는 역변환 거부 후 v0.16
재실행을 확인했다. 이는 복구 경로의 검증이며 전체 설치 전환이나 기본 앱 승격은 아니다.
[실제 복구 근거](../reports/camera_platform/install-v016/schema-recovery-live/live-recovery-summary.json)를 보존한다.

도메인 QA 재시작에서도 137개 기록·77개 작업·368개 감사 행·2,388개 Blob을
그대로 보존했다. 5개 도메인의 원래 결과를 GET으로 다시 확인했고, 실제 UI에서
동일 ISP 후보·프레임의 이미지와 지표를 확인했다. 전체 이력을 복원하는 조회는 느려서
동시 조회 일부가 제한 시간 안에 끝나지 않았다. 실패 시도도 보존했으며, 이후 순차
읽기 검증의 통과를 화면 응답성 합격으로 표현하지 않는다.
[재시작 도메인 검사](../reports/camera_platform/install-v016/domain-http/restart-campaign-summary.json),
[실제 UI 재조회](../reports/camera_platform/install-v016/domain-ui-restart-observations.json)를 구분한다.

검증용 서버와 작업용 브라우저 탭은 종료했다. 종료 중 프로세스 관찰 하위 명령이
한 번 실패한 기록은 그대로 남겼다. 당시 stderr가 저장되지 않아 원인은 미확정이다.
추가 종료 신호 없이 원래 PID·프로세스 그룹, 두 잠금, 닫힌 포트와 전체 데이터 보존을
재확인한 [최종 종료 확인](../reports/camera_platform/install-v016/domain-closure-final/verification.json)은
통과했다. 기본 앱과 기록은 변하지 않았다.

## 별도 개발 소스 0.16.1.dev1

이 설치 후보 이후 소스에서 아티팩트 소유자 조회와 케이스 목록 조회의 중복 해석을
각각 제거했다. 관련 시험 110개와 79개, 독립 검토를 통과했다. 복사 상태에서 해당
작업만 따로 측정한 중앙값은 각각 1.118→0.568초, 2.539→1.936초였다. 응답 바이트와
연결 증거 검증은 유지한다. 설치된 0.16.0의 코드·Wheel은 그대로이며, 새 소스의
설치 실행과 전체 UI 지연 개선은 아직 검증하지 않았다.
[소스 수정 범위](camera-platform-read-efficiency.md)를 따른다.

## 남은 제품 한계

광학은 centered even-asphere와 제한된 적용 범위의 PSF이며 일반 freeform,
tilt/decenter, 코팅·편광·미광은 남아 있다. 전하 수집·잔류·readout은 선언된
행동 모델이다. 전기 폐루프 AE, 임의 움직임의 HDR 정합·합성, LFM 최적화,
motion-aware ISP와 전역 다목적 최적화는 후속 범위다.

실제 UI에서 큰 계획 JSON을 펼쳤을 때 접근성 응답 지연, 완료 상세와 목록 상태의
일시적 불일치, 좁은 JSON 입력칸을 관찰했다. 고객용 UI 완료로 판단하지 않는다.
독립 실물 측정은 0건이고 물리 예측 검증과 총 불확실도는 미확립 상태다.
