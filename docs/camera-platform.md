# Camera Simulation & Validation Platform

**2026-09-28 기준 세 상태를 분리한다.** 기본 작업실 인도 기록은
[v0.15.0](../reports/camera_platform/delivery.json), 별도 R1 설치 후보는
[`0.16.1rc2`](camera-platform-r1-rc2-support.md), 현재 checkout과 별도 격리 설치
wheel의 개발 버전은 `0.16.2.dev35`다. RC2의 ZIP/wheel과 기본 작업실은 이 후속
기능으로 교체되지 않았다. 개발판은 기존 Lens×Sensor DB의 제한된 2×2 RAW·ISP·
화질 작업, HDR/LFM·인지 연결, [적응형 Scene 표본화·PSF 검증 경계](camera-platform-mtf-support-qualification.md)를
포함한다. [dev35 실행·설치 근거](camera-platform-physical-evidence-dev35.md)에
실제 수치 미수렴, 1024 입력의 자원 차단, 선택된 광선 재히스토그램 비교를
기록했다. 저장 기하 PSF convolution은 실제로 사용하지만 회절·절대 광량·
CRA×픽셀 방향 응답은 아직 제품 수준으로 결합되지 않았다. 원래
[우선순위](camera-platform-priority-20260925.md)의 보유 DB·평가·LUT·Agent 연결과
일반 광학·픽셀·전기 모델, HDR/LFM·고급 ISP·최적화는 계속 진행 중이다.
제품 승인과 실물 정확도 검증은 별도다.

아래는 버전별 작성 시점의 기록이다. 당시의 “현재” 표현을 오늘의 설치 지원으로
해석하지 않는다.

**2026-09-24 개발 소스 `0.16.1.dev13` 당시 기록.** dev13의 [PWM 노출 공간 진단](camera-platform-lfm-tradeoff.md)은 정책상 허용 노출과 실제 원본 RAW 목표 진입을 분리해 표시한다. 작성한 8 ms 단일 후보 사례는 목표에 0/4프레임 들어왔으므로 노출만 바꿀 대안이 없다. Gain/HDR 후보의 실제 비교나 물리 LFM 달성 가능성은 아직 미확립이다. dev12의 [잔류 PD 다중 노출 HDR](camera-platform-retained-hdr.md)은 trap 없는 정적 장면에서 현재 노출 광전자율을 추정하는 별도 opt-in 경로다. [당시 진행 기록](../reports/camera_platform/progress-20260924.md)과 [dev13 근거 요약](../reports/camera_platform/lfm-tradeoff-v0161/evidence-summary.json)을 따른다. 독립 실측 0건, 물리 정확도·LFM 성능 미확립, 총 예측 불확실도 미확인이다.

아래 dev16과 v0.15 설명은 각 시점의 기록이며 최신 개발 소스 상태는 위 링크를 따릅니다.

**개발 소스 v0.16.0.dev16의 제한된 수치·API·브라우저 검증을 확인했습니다.** [잔류 RAW의 공간·시간 ISP와 후보 비교](camera-platform-retained-isp.md)는 행별 전하 전달 시각과 원본 RAW를 보존하며 코드 영역에서 처리합니다. 실제 브라우저에서 ISP 모델 등록·재처리·6후보 비교를 실행했습니다. 회귀 근거는 전체 기준 실행과 마지막 좁은 API 수정의 집중 재검증을 합친 범위이며, 그 수정 후 전체 시험을 다시 실행한 것은 아닙니다. 재시작·백업·정리의 최종 상태는 [dev16 근거 요약](../reports/camera_platform/retained-isp-v016/evidence-summary.json)을 따릅니다. 물리적 잔류 전하 복원이나 실측 화질 검증을 뜻하지 않습니다.

**dev16 당시 우선순위는 광학·픽셀·전기·ISP 확장을 설치 가능한 v0.16으로 통합하는 것이었습니다.** 이후 별도의 v0.16 설치 후보와 dev11까지의 전기·HDR/LFM 소스 개발을 진행했습니다. 기본 설치본은 아래 v0.15를 유지하며, 원문 일곱 모듈·네 실행 방식의 전체 상용화 Goal은 미완료입니다.

**현재 인도 버전은 v0.15.0입니다.** [사례별 설계 검토](camera-platform-cases.md)를 포함한 기본 설치본이며, [최신 인도 기록](../reports/camera_platform/delivery.json)을 따릅니다. v0.16 개발 소스는 [전기·순차 HDR](camera-platform-electrical-hdr.md), [조리개 투과율](camera-platform-stop-transmission.md), [Native 4사이트 응답](camera-platform-native-pixels.md), [공유 전하 DCG](camera-platform-dcg.md), [동일 RAW의 고급 ISP](camera-platform-advanced-isp.md)를 확장합니다. 소스 검증과 기본 설치본의 기능을 구분해야 합니다.

**이전 v0.14.0 인도 기록입니다.** [처방 OPD·위치별 PSF](camera-platform-prescription-optics.md)를 기본 작업실에 연결했습니다. 같은 처방의 광량·입사각 응답과 PSF를 새 Lens/Camera로 등록하고 실제 64×64 RAW/RGB와 RAW 보존 ISP 재처리를 실행했습니다. [실행 근거](../reports/camera_platform/prescription-primary-delivery.json)와 [기본 화면 조회](../reports/camera_platform/prescription-ui/primary-v014/verification.json)를 확인합니다.

[백업·설치 전환](../reports/camera_platform/primary-prescription-upgrade.json)은 기존 **215개 기록·74개 작업·437개 감사 이벤트**를 보존했습니다. [기존 시간 촬영](../reports/camera_platform/primary-prescription-legacy-rerun.json)의 RAW/RGB가 동일하며 [최종 무결성](../reports/camera_platform/primary-prescription-integrity.json)을 확인했습니다. 원래 상용화 Goal은 계속 진행 중입니다. 실측은 **0건**, 총 예측 불확실도는 **unknown**이고 독립 고객 환경·일반 물리 모델·권한·Agent 흐름 등이 남아 있습니다. [최신 인도 기록](../reports/camera_platform/delivery.json)과 [요구사항 감사](camera-platform-requirements-audit.md)를 따릅니다.

아래 v0.13 및 이전 버전 설명은 각 기능의 도입 당시 근거입니다.

**이전 v0.13.0 인도 기록입니다.** [저장된 분광 복사휘도 재사용](camera-platform-scene-projection.md)을 기본 설치 작업실에 연결했습니다. 성공한 장면의 원본 `Lλ`를 보존하고, 같은 시점·초점거리·Native 격자·파장에서 새 투과율과 조리개로 센서면 광량을 계산한 뒤 대상 카메라의 RAW/RGB를 생성합니다. 조건이 달라지면 재렌더링을 요구하며, 원본 렌더링과 새 변환의 출처·환경을 별도 보존합니다.

[v0.12→v0.13 설치 전환](../reports/camera_platform/primary-projection-upgrade.json)은 백업을 만들고 기존 **212개 기록·71개 작업·425개 감사 이벤트**를 보존했습니다. [기본 작업실의 재사용 실행](../reports/camera_platform/primary-projection-verification.json) **9항목**, [기존 시간 RAW/RGB 재실행](../reports/camera_platform/primary-projection-legacy-rerun.json), [후속 무결성 검사](../reports/camera_platform/primary-projection-integrity.json)를 확인했습니다. 새 예제와 재실행은 **3개 기록·3개 작업·12개 감사 이벤트**를 추가했고 원래 기록은 유지했습니다. 기본 작업실의 [읽기 전용 화면 검사](camera-platform-installed-runtime.md#기본-작업실의-화면-검증)는 **7개 흐름·168항목**을 통과했습니다(POST 0회·브라우저 오류 0건).

`ready`와 계산 성공은 제한된 입력·실행 계약을 확인한 상태입니다. 실물 예측 정확도는 미확립이고 총 불확실도는 `unknown`이며, 같은 원본에서 재사용한 MC 표준오차는 공유 표본의 수치 진단입니다. 실제 장비 측정 데이터는 **0건**입니다. 독립 실측, 일반 광학/픽셀/전기 모델·HDR/LFM·고급 최적화·Agent·권한·고객 환경 운영을 포함한 **원문 일곱 모듈·네 실행 방식의 전체 Goal은 미완료**입니다. 설치 검증은 기존 계산 의존성을 공유하며 깨끗한 OS 설치를 보장하지 않습니다.

## v0.13 검증 근거

원본 투과율이 0인 파장도 기존 광량을 나누어 복구하지 않고 저장 `Lλ`에서 계산합니다. 장면·격자·온도 등 조건이 달라지면 재렌더링을 요구하며, 이 기능은 일반적인 단계별 DAG Cache 전체를 구현한 것은 아닙니다.

[개발 회귀](../reports/camera_platform/projection-development-tests.xml) **1,216개**와 [소스·환경 결합 기록](../reports/camera_platform/projection-development-test-binding.json), [전환 시험](../reports/camera_platform/projection-runtime-transition-tests.xml) **78개**와 [직접 입력 해시 기록](../reports/camera_platform/projection-runtime-transition-tests-binding.json)을 확인했습니다. 전환 시험의 기존 36개는 전체 회귀에 포함되어 있으며 42개가 별도 추가분입니다. [화면 상태 회귀](../reports/camera_platform/scene-projection-ui-state-regression.json) **6항목**, [별도 개발 브라우저](../reports/camera_platform/projection-ui/development/verification.json) **26항목**도 통과했습니다.

[v0.13 패키지 검사](../reports/camera_platform/release-archives/v0.13.0/package-verification.json)는 Checkout 밖 설치 실행, 소스·정적 파일 **132개** 일치와 저장 광휘 → 실제 RAW 경로를 확인했습니다. [설치본 격리·재시작](../reports/camera_platform/projection-installed-runtime-verification.json) **17항목**은 개발 Checkout 변경·재시작 뒤 원본/새 광량·MCSE·RAW/RGB의 7개 해시 일치를 확인했고 검사 서버의 종료도 확인했습니다. 별도 설치 QA의 [광휘 재사용](../reports/camera_platform/projection-ui/installed-v013/verification.json) **26항목**, [카메라 준비](../reports/camera_platform/projection-ui/preparation-v013/verification.json) **20항목**, [장면 작성](../reports/camera_platform/projection-ui/authoring-v013/verification.json) **36항목**도 통과했습니다. 이 검사는 기본 작업실의 읽기 전용 화면 검사와 구분합니다. 자세한 적용 범위는 [기능 문서](camera-platform-scene-projection.md#검증-근거와-인도-상태)를 따릅니다.

[최종 인도 입력 감사](../reports/camera_platform/projection-release-input-audit.json) **14항목**은 소스·정적 파일 132개와 Wheel·설치본, 시험의 소스/환경 결합 및 v0.12 보관 51개 파일의 불변을 확인했습니다. [최종 Runtime 상태](../reports/camera_platform/projection-v013-runtime-state.json)는 별도 QA 서버·Worker 종료와 기본 작업실 정상 실행을 확인합니다.

## 이전 인도 기록

**v0.12.0 인도 당시 기록입니다.** [장면별 카메라 준비](camera-platform-scene-camera.md)를 기본 설치 작업실에 연결했습니다. 모델·촬영 조건을 저장 없이 검사하고 원본 구성·모델을 보존한 새 구성을 만들어 Mitsuba → PSF → RAW/RGB로 실행합니다. 준비 가능 오판을 실제 Runtime 계약 검사로 막고, 빈 모델 선택과 잘못된 JSON의 화면 복구, Worker의 모든 종료 경로에서 자식 프로세스 정리를 보완했습니다.

[회귀 시험](../reports/camera_platform/preparation-v012-tests.xml) **1,158개**, [별도 전환 시험](../reports/camera_platform/scene-preparation-transition-tests.xml) **36개**, [설치본 실행·재시작](../reports/camera_platform/preparation-installed-runtime-verification.json) **14항목**을 통과했습니다. 전환 시험 중 6개는 전체 회귀 시험과 겹치므로 두 수를 독립 시험 수로 합산하지 않습니다. 별도 설치 QA의 [장면 작성](../reports/camera_platform/preparation-ui/authoring-v012/verification.json) **36항목**과 [카메라 준비](../reports/camera_platform/preparation-ui/installed-v012/verification.json) **20항목**을 확인했습니다.

[기본 설치 전환](../reports/camera_platform/primary-preparation-upgrade.json)은 기존 **208개 기록·68개 작업·412개 감사 이벤트**를 보존했습니다. [기본 작업실의 새 구성 실행](../reports/camera_platform/primary-preparation-verification.json) **7항목**은 준비 → 실제 Mitsuba → RAW/RGB와 원본 해시·근거 ZIP을 확인합니다. 기본 작업실의 읽기 전용 화면 검사도 **6개 흐름·141항목**을 통과했습니다(POST 0회·브라우저 오류 0건). [흐름별 화면 근거](camera-platform-installed-runtime.md#검증과-한계)를 별도 QA 결과와 구분합니다. [최종 보존 검사](../reports/camera_platform/primary-preparation-integrity.json)와 [기존 시간 촬영 재실행](../reports/camera_platform/primary-preparation-legacy-rerun.json)은 원본 기록과 RAW/RGB 바이트가 유지됨을 확인합니다.

`ready`는 제한된 실행 인터페이스가 맞는다는 의미입니다. 8×8의 영 광량 입력으로 모델 연결만 검사하며 실제 장면 복사휘도·광학 근사·실물 정확도를 검증하지 않습니다. 실제 장비 측정 데이터는 **0건**입니다. 독립 실측·총 불확실도, 일반 광학/픽셀/전기 모델·HDR/LFM·일반 최적화·Agent·권한·고객 환경 운영을 포함한 **원문 일곱 모듈·네 실행 방식의 전체 Goal은 미완료**입니다.

**이전 인도·개발 기록.** v0.11.0은 3D 자산 작성·분광 렌더링·PSF/RAW/RGB를 인도했고, 당시 회귀 1,115개와 기본 화면 118항목 및 기존 191개 기록·62개 작업·377개 감사 이벤트 보존을 확인했습니다. [v0.11 인도 보관본](../reports/camera_platform/release-archives/v0.11.0/delivery.json)을 따릅니다. 그 뒤의 개발 시험 **1,143개·브라우저 17항목**은 v0.12 패키지 인도 전의 역사적 결과이며, 위 최종 검증 근거와 구분합니다.

**v0.10.0 인도 당시 기록:** [RAW 응답 취득·공통 관측·분석 버전](camera-platform-response-ingest.md)을 기본 작업실에 설치했습니다.
원본 JSON 파일 바이트와 실제 RAW를 보존하며 같은 취득을 여러 지표로 분석하고 비교합니다.
플랫폼 회귀 시험 1,089개·전환 경합 2개, 설치본 실행/재시작 9항목과 설치본 브라우저 검사를 확인했습니다.
기존 기록 182개·작업 60개·감사 이벤트 362개와 시간 RAW/RGB를 보존했습니다.
독립 실측·총 불확실도와 전체 일곱 모듈·네 실행 방식의 상용화 Goal은 계속 진행 중입니다.

**v0.9.0 도입 기록.** [AE/AWB 제어](camera-platform-closed-loop.md)는
불변 정책·촬영 프로그램, 전체 노출/Gain 범위 검사, 공통 시험·API·SDK·별도 Worker,
32프레임 촬영과 RAW 재처리를 작업실에서 연결합니다. 요청·양자화·실제 적용과 목표 잔차를
보존하며, 현재 폐루프 관측을 기존 비교기로 물리 검증 PASS 처리하지 않습니다.
[패키지 검증](../reports/camera_platform/package-verification.json),
[설치본 격리·재시작 검증](../reports/camera_platform/controls-installed-runtime-verification.json),
[기존 기록 보존 전환](../reports/camera_platform/primary-controls-upgrade.json)을 확인했습니다.
전체 상용화 Goal은 진행 중이며 독립 실측, 일반 3D/물리 모델, HDR/LFM, 전체 불확실도,
고급 최적화·Agent·권한·고객 환경 배포는 남아 있습니다.

아래 버전별 설명은 각 기능의 도입 시점과 당시 검증 범위를 보존한 기록입니다.
최신 인도 근거는 [delivery.json](../reports/camera_platform/delivery.json)의 버전과 해시를 따릅니다.

**v0.8.1 도입 기록.** [작업실 조회 경량화](camera-platform-workbench.md)는
작업·모델·예측 이력의 명시적 요약, 선택 원본 조회, 작은 SDK Session과 검증된 일괄 DB
읽기를 추가합니다. 원본·계산·물리 검증 범위는 보존하며 전체 상용화 Goal은 진행 중입니다.
아래 v0.8.0 이하 설명은 해당 기능이 추가된 버전의 기록입니다.

**v0.8.0 도입 기록.** [수치 지표 Surrogate](camera-platform-surrogate.md)는
완료된 설계 실험의 학습/검증 조건 선택, 불변 Worker 학습, 사전 고정 오차 기준,
모든 모서리를 포함한 입력 범위 검사와 최대 4,096개 일괄 예측을 연결합니다.
계수·원본 Run 해시·검증 잔차·예측 이력을 보존하며, 같은 조건의 새 Run ID도
검증 중복으로 거부합니다. 독립 실물 검증이나 연속 영역의 오차 보장은 아닙니다.
[회귀시험](../reports/camera_platform/surrogate-tests.xml) 962개,
[실제 HTTP/Worker](../reports/camera_platform/surrogate-http-verification.json) 12항목,
[브라우저](../reports/camera_platform/surrogate-ui/headless-qa.json) 18항목과
[Checkout 밖 설치 실행](../reports/camera_platform/package-verification.json)을 확인했습니다.
[기본 작업실 전환](../reports/camera_platform/primary-surrogate-upgrade.json)은 기존 기록과
시간 RAW/RGB 해시를 보존합니다. 전체 Goal은 진행 중이며, 수치 지표 근사를 일반
광학/픽셀/영상 Surrogate나 상용제품 완료로 계산하지 않습니다.

아래 v0.7 이하 설명과 상세 표는 기반 기능의 역사적 범위를 포함합니다.
최신 버전별 근거는 [인도 기록](../reports/camera_platform/delivery.json)을 따릅니다.

v0.7 인도에는 다음 기능이 추가되었습니다. [설계 변수 탐색](camera-platform-studies.md)은
일곱 입력의 새 모델·시험 버전, 전체 계획 사전 검사와 원자적 작업 제출, 조합별 기준점,
수치 제약·표본 Pareto 비교·구간 변화율·근거 JSON을 연결합니다. Quick/평탄장 Response-DB의
유한 설계 공간이며 일반 Lens/Pixel/ISP/Scene 최적화, 공차·전역 민감도, Surrogate와
Agent의 완료를 뜻하지 않습니다. 원문 일곱 모듈·네 실행 방식의 범위를 유지합니다.
[회귀 시험](../reports/camera_platform/studies-tests.xml) 937개,
[실제 HTTP/Worker](../reports/camera_platform/studies-http-verification.json) 5항목,
[브라우저](../reports/camera_platform/studies-ui/headless-qa.json) 21항목과
[설치 패키지](../reports/camera_platform/package-verification.json)의 실제 실행을 확인했습니다.
[기본 작업실 전환](../reports/camera_platform/primary-studies-upgrade.json)은 기존 기록과
시간 RAW/RGB를 보존했습니다. 전체 물리 검증과 불확실도는 여전히 미확립입니다.

직전 v0.6 인도에는 다음 경로를 확인했습니다. [입력 경계 전파](camera-platform-uncertainty.md)는
Quick/정적 Response-DB에서 선언한 모델·시험 경계를 기대 광전자·RAW 신호·채널 비율과
동일 CFA/ROI 관측으로 연결합니다. 전체 모델·수치·관측 불확실도는 여전히 `unknown`이며
PSF·시간·ISP 전파와 독립 실물 검증은 남아 있습니다.
[회귀 시험](../reports/camera_platform/uncertainty-tests.xml) 914개,
[HTTP/Worker](../reports/camera_platform/uncertainty-http-verification.json) 6항목,
[설치 패키지](../reports/camera_platform/package-verification.json)와
[기본 작업실 v0.6 전환](../reports/camera_platform/primary-uncertainty-upgrade.json)을 확인했습니다.
기본 화면은 새 경계 9항목·기존 시간 기록 31항목을 읽기 전용으로 검사했습니다.
아래 v0.4/v0.5 설명과 시험 수는 해당 버전의 역사적 범위이며,
현재 근거는 [인도 기록](../reports/camera_platform/delivery.json)의 버전·보고서를 따릅니다.

CameraE2E의 기존 계산 코드를 재사용하는 **로컬 엔지니어링 에디션**입니다. 설계 특성화 → 모델 등록 → 버전 고정 카메라 구성 → 시험/실험 → 실제 RAW/RGB 계산 → RAW 관측 비교 → 보정 후보 → 근거 패키지를 하나의 API와 작업실로 연결합니다. 시간 장면과 프레임별 적용 설정을 고정한 촬영, 원본 RAW를 보존하는 ISP 재처리도 연결되어 있습니다.

Model Factory는 실제 Prysm의 선언한 동공→수신 픽셀 적분 PSF, RayOptics의 제한된 구면 렌즈 특성화, 별도 Meep의 평면층 광학 계산, 조건을 가진 응답 테이블 등록, RAW의 제한된 R/B 각도 응답 보정 후보를 지원합니다. 첫 계산 사례는 **렌즈 A/B의 입사각 분포에 따른 센서 주변부 RAW 색 비율 변화**입니다. 예제 모델·처방·광원은 가정 데이터이며 실제 제품·실측을 의미하지 않습니다. 전체 플랫폼 범위와 남은 작업은 [원문 요구사항 감사](camera-platform-requirements-audit.md)에 유지합니다.

## 실행

이 저장소에서 준비된 실행 환경:

```bash
.venv-platform/bin/python tools/start_camera_platform.py
```

RayOptics·Prysm 선택 설치는 `pip install -e '.[platform,factory-rayoptics,factory-prysm]'`입니다. Prysm 0.21.1은 legacy 패키지이므로 단독 설치 시 `--use-pep517` 빌드 옵션을 사용하며, 런타임의 setuptools 범위는 `>=80,<81`입니다. Meep은 플랫폼 Python과 분리된 Meep 1.33.0 serial 환경을 사용합니다. 서버를 처음 시작할 때 다음과 같이 검증·저장하면 Finder 재실행에도 적용됩니다.

```bash
.venv-platform/bin/python tools/start_camera_platform.py \
  --meep-python /absolute/path/to/meep-env/bin/python
```

지원 환경과 설정 우선순위는 [Meep Factory 계약](camera-platform-meep-factory.md)을 확인합니다. 실행 파일 경로는 로컬 런처 설정이며 API 작업 입력으로 받지 않습니다.

Finder에서는 [Start Camera Platform.command](../simulations/camera_platform/Start%20Camera%20Platform.command)를 실행합니다. 런처는 터미널과 분리된 서버를 시작하고 주소·workspace·state directory·Worker 상태를 확인한 뒤 브라우저를 엽니다.

검증된 설치 버전을 선택한 작업실은 해당 패키지의 코드·화면·데이터로 실행됩니다.
개발 Checkout과 분리하는 방법과 이전 버전 선택·복구 절차는
[설치 Runtime](camera-platform-installed-runtime.md)을 확인합니다. Python 의존성은
현재 환경을 공유하며 깨끗한 OS 설치 검증은 별도로 남아 있습니다.

- 작업실: `http://127.0.0.1:8770/`
- API 문서: `http://127.0.0.1:8770/docs`
- 기계 판독 스키마: `http://127.0.0.1:8770/openapi.json`
- 서버/계산 로그: `outputs/camera-platform/server.log`, `worker.log`, `worker-supervisor.log`
- 불변 기록/작업 큐: `outputs/camera-platform/registry.sqlite`
- 원본 산출물: `outputs/camera-platform/blobs/<sha256>`

새 환경에는 Python 3.12 이상에서 설치합니다.

```bash
python3.12 -m venv .venv-platform
.venv-platform/bin/python -m pip install -e '.[platform,dev]'
.venv-platform/bin/python tools/start_camera_platform.py
```

현재 개발 환경은 기존 `isetcam-py`의 계산 의존성을 공유하는 별도 venv입니다. 배포 환경은 위 설치 절차 또는 검사된 wheel을 사용해 독립적으로 준비해야 합니다. 개발 환경이 배포 이미지와 같다고 가정하지 않습니다.

직접 실행하거나 외부 Worker로 분리할 수 있습니다.

```bash
PYTHONPATH=src .venv-platform/bin/python -m camera_platform serve

# API와 Worker를 각각 관리할 때
PYTHONPATH=src .venv-platform/bin/python -m camera_platform serve --external-worker
PYTHONPATH=src .venv-platform/bin/python -m camera_platform worker

# 합성 예제가 없는 별도 저장소
PYTHONPATH=src .venv-platform/bin/python -m camera_platform serve \
  --state-dir /absolute/path/to/project-state --port 8771 --empty
```

API는 127.0.0.1에만 바인딩됩니다. `--external-worker`에서는 런처의 관리 Worker 검사 대신 운영자가 Worker 프로세스를 별도로 감시합니다. 소스/계산 의존성이 서버 시작 뒤 바뀌면 새 작업 제출을 거부합니다. 서버를 다시 시작해 현재 코드를 고정한 뒤 실행합니다.

## 첫 사용 흐름

1. **사례 개요**에서 질문과 모델 출처, 적용 범위를 확인합니다.
2. **카메라 구성**에서 Lens·Sensor·ISP 모델 버전을 선택합니다. 단위·광학 범위·RAW 계약·중복 물리 효과가 맞아야 계산할 수 있습니다.
3. **실험**에서 같은 시험의 Lens A/B를 실행하거나 4/8 ms 노출 sweep을 제출합니다. 계산은 별도 프로세스에서 수행됩니다. 그래프의 실제 노출·Gain과 선택한 Run ID를 확인합니다.
4. **Validation**에서 JSON 측정 파일 또는 unpacked RGGB NPY RAW/Dark와 취득 metadata를 불러옵니다. RAW 분석은 ROI/CFA/Black 또는 Dark 보정 방법을 고정한 새 시험을 만듭니다. 반환된 시험으로 계산을 다시 실행합니다. 형식 예제는 `estimated`이며 실제 측정으로 저장하지 않습니다.
5. Run·Measurement·Test를 선택해 비교합니다. 다른 구성/조건/단위/좌표라면 `INVALID_COMPARISON`, 적용 범위 밖이면 `OUT_OF_DOMAIN`, 필요한 근거나 불확실도가 없으면 `INCONCLUSIVE`입니다.
6. **Model Factory**에서 Solver와 명시적 입력 처방을 선택해 별도 작업을 실행합니다. RayOptics의 수치 검사에 통과한 분광·각도 렌즈 후보를 고정된 적용 조건으로 등록하고 Builder에서 Sensor/ISP와 조합합니다. Meep의 signed R/T/A와 층별 flux는 광학 근거로만 보존하며 수집전자 QE로 등록하지 않습니다. 응답 테이블 등록과 RAW/Dark 기반 R/B 각도 응답 보정도 지원합니다. 학습 데이터는 독립 물리 검증에서 제외됩니다.
7. **Releases**에서 계산/검증 근거를 묶습니다. 미충족 조건은 차단 사유로 보존합니다. JSON은 구조화된 manifest이고 **원본 포함 ZIP**은 RAW/RGB 배열·이미지·실측 원문을 포함합니다.

**판정과 승인:** 수치상 비교 PASS, 독립 실물 검증 PASS, 최종 배포 승인은 다른 상태입니다. 현재 Runtime은 예측 불확실도를 `unknown`으로 기록하므로 실제 실측을 추가해도 자동으로 Physical Validation PASS가 되지 않습니다. 전체 불확실도 전파·독립 보정 검증 경로가 필요합니다. Release는 최대 `review_required`까지 준비하며 최종 승인을 대신하지 않습니다.

## 보장하는 데이터 경계

- 자산·모델·조합·시험·실측·검증·릴리스 기록은 수정/삭제 API가 없는 불변 revision입니다. 완료된 작업 기록도 DB trigger로 수정/삭제를 거부합니다.
- JSON 기록은 canonical hash, RAW/RGB/실측 원문은 SHA-256으로 식별합니다. 내려받기와 내보내기에서 파일을 검증합니다.
- 감사 로그는 append-only hash chain입니다. 로컬 무결성 확인용이며 외부 서명·법적 공증·관리자 변조 방지 저장소가 아닙니다.
- Cache는 입력 데이터, 정확한 모델/시험 snapshot, 실행 옵션, seed, Python/의존성 버전, 실제 계산 소스 hash에 의존합니다. 아직 단계별 DAG cache는 없습니다.
- 작업 상태는 queued/running/succeeded/failed/cancelled입니다. 단일 Worker가 원자적으로 claim하며 최대 64개 활성 작업, 실행당 120초 제한을 둡니다. 재시작 시 중단된 작업은 실패로 남기고 입력을 보존합니다.
- `/measurements/import`는 업로드 원문 UTF-8 JSON byte를 보존합니다. `/measurements`를 통한 객체 등록은 canonical contract JSON을 보존하며 원문 파일과 같다고 표시하지 않습니다.
- 기존 `camerae2e_db/catalog.sqlite`는 `mode=ro`로만 엽니다. 이전 Design Workbench와 현재 플랫폼 state는 분리됩니다.

평탄장 Response-DB의 64×64 출력은 **정규화한 전체 시야의 대표 샘플 격자**에 native pixel의 광전자 계산을 적용한 것입니다. 공간·시간 Runtime의 Native crop과 다릅니다. RAW 관측 비교는 대표 출력 이미지 대신 연속 기대 신호에 측정의 실제 native crop/ROI/CFA 가중치를 적용합니다. RGB는 Software ISP의 센서 채널 시각화이며 보정된 색채계 sRGB가 아닙니다.

RAW 원본은 `[frames, rows, columns]` 정수 NPY 배열이며 RAW+Dark 합계 64 MiB, 최대 256 프레임을 지원합니다. Python 객체나 임의 RAW packing을 해석하지 않습니다. 실제 측정은 센서 크기·pitch와 일치하는 native crop geometry, 장비·교정·Serial·취득 조건을 요구합니다. 동일 RAW 프레임의 재등록은 ID/NPY 형식/프레임 순서를 바꿔도 거부합니다. 분석값과 출처를 변경한 JSON을 원래 RAW에 연결할 수 없으며 비교·보정에서 원본 재분석을 확인합니다. 새 픽셀 편집이나 새로운 센서 취득의 진위를 인증하는 기능은 아닙니다.

ROI 반복 표준오차는 진단값으로 제공합니다. 이를 전체 물리 오차의 절대 경계로 바꾸지 않습니다. ADC 끝값 검사로 확인할 수 없는 full-well 비선형성은 별도 실측 근거가 필요합니다. 상세 입력은 [RAW 관측 계약](camera-platform-measurement.md), 후보 생성은 [보정 계약](camera-platform-calibration.md)을 확인합니다.

## Python SDK / Agent

Agent도 같은 버전 API를 사용합니다. 플러그인 코드를 API 요청에 넣어 임의 실행하는 기능은 제공하지 않습니다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient() as platform:
    info = platform.get('bootstrap')
    assembly_id = info['demo']['assembly_ids'][0]
    test_id = info['demo']['test_id']

    preflight = platform.post('assemblies/check', {'assembly_id': assembly_id})
    run = platform.submit(assembly_id, test_id,
                          parameters={'exposure_ms': 4.0, 'seed': 42})
    result = platform.wait(run['id'])
    print(result['status'], result['result']['channel_ratios'])

    # 등록된 실제 데이터가 있을 때만 비교합니다.
    # report = platform.compare(run['id'], measurement_id, test_id)
```

`GET /api/v1/templates/model`, `GET /api/v1/templates/measurement`, `GET /api/v1/templates/capture`는 현재 ID를 포함한 입력 형식을 반환합니다. 등록 예제에서 `source_kind`, 실제 장비·교정·Serial·조건·불확실도를 사실대로 작성해야 합니다. 알 수 없는 항목은 추측으로 대체하지 않습니다.

```python
# metadata에는 실제 취득 조건/관측 방법을 명시합니다.
with PlatformClient() as platform:
    captured = platform.import_capture(metadata, raw_path, dark_path=dark_path)
    observed_run = platform.wait(platform.submit(
        captured['measurement']['assembly_id'], captured['test']['id'])['id'])
    report = platform.compare(observed_run['id'], captured['measurement']['id'],
                              captured['test']['id'])
```

외부 솔버를 연결할 때는 [Runtime 계약](camera-platform-runtime.md)과 [아키텍처](camera-platform-architecture.md)를 기준으로 입출력 축·단위·물리 효과·자원 한계를 선언합니다. 지원하지 않는 물리 입력을 조용히 근사하지 않습니다.

## 공간 영상 계산과 RAW 재처리

v0.4는 `spatial_scene_v1`과 `isp_replay_v1`을 추가합니다. 분광 영상은 **PSF·상대 조도 적용 전 센서 이상 영상면의 W/m²/nm** 입력입니다. RGB 사진에서 분광 정보를 복원하거나 3D 장면을 렌더링하지 않습니다. 파장 축, 실제 센서 크기·픽셀 간격·Crop 원점, 포함된 효과와 광학 조건을 고정합니다.

1. **Model Factory → Prysm**에서 선언된 동공의 PSF를 계산합니다. 후보가 만들어지면 같은 Lens 자산의 기존 RI·입사각 응답과 결합합니다. 부모 모델의 유효 조건을 좁힐 수는 있지만 넓히거나 누락할 수 없습니다. 이 동공은 RayOptics 처방에서 추출한 파면이 아니며, 조합은 독립적으로 선언한 가정입니다.
2. **장면 등록**에서 `spectral_chart`, `slanted_edge`, `point`, `flat` 중 명시적인 합성 영상을 만들거나, NPY와 출처·단위·Sampling metadata를 업로드합니다. 기본 차트는 3 µm 센서의 중앙 64×64 Crop이며 전체 센서 리사이즈가 아닙니다.
3. Builder에서 PSF Lens·Sensor·ISP 구성 버전을 등록합니다. **실험 → 공간 계산**에서 장면과 구성을 선택하고 **시험 정의를 불러와 등록**한 다음 실행합니다. 기대 RAW 채널 평균은 기존 평탄장 R/G·B/G 시험과 별개입니다.
4. 결과의 RAW, 모든 프레임의 ISP 중간 단계, RGB, PSF·조도·전자 지도와 원본 장면을 내려받습니다. 이미지 경계 바깥은 0이며, 국소 공간 불변 PSF와 셀 중심 광속 표본화의 가정을 보존합니다.
5. **ISP 프로파일**에서 고정 WB·CCM·Tone 파라미터를 새 모델로 등록합니다. **저장된 RAW 재처리**에서 완료된 Camera Run 또는 등록된 Capture를 선택합니다. 원본 RAW 바이트·촬영 조건·프레임 순서·Crop 위상은 그대로 유지하며, 재처리 이력은 Camera Run과 별도입니다.

Prysm은 파장별 동공/수신 픽셀 격자를 독립적으로 세분화해 비교하고, 유한 PSF 창에 남은 에너지와 정규화 사실을 기록합니다. 복소 Kernel OTF에는 픽셀 개구가 포함되므로 Lens 자체의 Optical MTF나 측정 SFR로 표시하지 않습니다. 수치 통과 상태와 물리 검증·불확실도·승인은 별도입니다.

```python
with PlatformClient() as platform:
    # assembly_id는 명시적인 PSF Lens를 포함하는 기존 구성입니다.
    scene = platform.create_spectral_fixture(
        platform.template('spatial-scene', assembly_id=assembly_id))
    test = platform.post('tests', platform.template(
        'spatial-test', assembly_id=assembly_id, scene_id=scene['id']))
    run = platform.wait(platform.submit(
        assembly_id, test['id'], plugin_id='spatial_scene_v1', scene_id=scene['id'])['id'])
    profile = platform.template('isp-profile', assembly_id=assembly_id)
    profile['payload']['white_balance']['gains_rgb'] = [1.3, 1.0, 0.75]
    isp = platform.post('models', profile)
    replay = platform.wait_replay(platform.submit_replay('run', run['id'], isp['id'])['id'])
```

계산 정의·지원 한계: [공간 Runtime](camera-platform-spatial.md), [Prysm PSF Factory](camera-platform-psf-factory.md), [ISP 재처리](camera-platform-isp-replay.md). 원본 Scene 조건이 필수 Test 조건을 포함하지 않으면 실행을 거부합니다. 보존된 RAW에 다른 노출·센서·잡음 Seed를 적용하는 요청도 재처리 입력으로 받지 않습니다.

## 시간 장면과 프레임별 적용 설정

1. PSF Lens를 포함한 구성을 준비합니다. 여러 노출을 사용할 새 Lens 후보에는 Prysm 등록의 **광학 응답 · Sensor 노출/Gain과 독립**을 선택할 수 있습니다. 기존 후보의 고정 조건은 바뀌지 않고 부모 Lens·Sensor·ISP의 유효 범위도 유지됩니다.
2. **Model Factory → 움직임·점멸과 프레임 설정**에서 합성 이동광점·점멸 장면을 만들거나 기존 분광 영상 ID를 상대 시각에 연결한 JSON을 등록합니다. 모든 영상은 같은 파장·크기·Pitch·Native crop을 사용합니다. 합성 장면의 작성 파라미터와 NPY 원본을 함께 보존합니다.
3. 연결된 장면에 프레임별 설정을 등록합니다. Clock, Frame ID, 시작 시각, 실제 적용 노출(ns)·Gain·ISP를 명시합니다. 명령 기록과 실제 적용값은 별도이며, 없는 명령은 필드를 생략하고 알 수 없는 지연은 미확정으로 유지합니다. 파일 업로드는 CRLF를 포함한 UTF-8 원문을 보존합니다.
4. **실험 → 시간 장면 · 프레임별 촬영**에서 두 시퀀스의 시험을 생성·등록한 뒤 실행합니다. 노출·Gain·프레임 수를 별도 스칼라로 덮어쓰지 않습니다. 프레임 선택기는 저장 영상과 해당 프레임의 실제 적용값을 같이 표시합니다.
5. **이 RAW를 다른 ISP로 재처리**하면 모든 원본 RAW와 Clock·프레임별 촬영 설정을 유지한 채 선택한 ISP를 적용합니다. 취득 ISP와 재처리 ISP를 구분해 표시합니다.

시간 Runtime은 선언된 선형/ZOH 분광 영상 사이를 적분합니다. Rolling shutter는 전체 센서의 행 번호와 Crop 원점을 사용하고, 적분 뒤 프레임당 한 번만 Dark·Noise·Full well·Gain·ADC를 적용합니다. 프레임 간 분산을 시간 잡음이나 SNR로 표시하지 않습니다. 기본 Gain 예제는 모델 유효 범위상 1이며, 숫자 변경만으로 다른 Gain의 모델 근거가 생기지 않습니다.

3D 움직임·가림, 폐루프 AE/AWB, HDR Merge와 실제 장비 지연 모델은 후속 범위입니다. 점멸 예제는 주기당 최소 8개 표본을 요구하지만, 이 조건을 물리 정확도 보증으로 해석하지 않습니다. 기록된 시각을 보존하는 것과 실제 하드웨어 동기 정확도를 검증하는 것은 별개입니다.

자세한 계산·SDK·API는 [시간 Runtime](camera-platform-temporal.md)과 [Clock·설정 계약](camera-platform-timing.md)을 확인합니다.

## 검증과 배포 경계

```bash
# 도메인·물리 계약·무결성·API 테스트
PYTHONPATH=src .venv-platform/bin/python -m pytest tests/platform -q

# 실제 HTTP → 별도 Worker → CameraE2E → 원문 보존 → 검증/ZIP → 재시작
.venv-platform/bin/python tools/verify_camera_platform.py

# 신뢰할 수 있는 Meep 환경을 지정한 실제 두 Solver → 모델 재사용 검사
CAMERA_PLATFORM_MEEP_PYTHON=/absolute/path/to/meep-env/bin/python \
  .venv-platform/bin/python tools/verify_camera_factory.py

# 실제 Prysm → native Scene → RAW/RGB → ISP 재처리 → 원본 ZIP → 재시작
.venv-platform/bin/python tools/verify_camera_imaging.py

# 현재 저장소의 기록·파일·감사 chain 검사
PYTHONPATH=src .venv-platform/bin/python -m camera_platform verify

# API 스키마 export
PYTHONPATH=src .venv-platform/bin/python -m camera_platform export-openapi \
  --output reports/camera_platform/openapi.json
```

근거 ZIP과 운영 백업은 별도 기능입니다. Release ZIP에는 Factory 처방·실행 환경·수치 결과·원본 배열과 보정 후보의 부모 모델·학습 RAW/측정/시험을 포함합니다. 운영 백업은 SQLite의 일관된 snapshot과 참조 blob을 검사하고, 복원 시 존재하지 않는 새 디렉터리에만 원자적으로 게시합니다.

```bash
PYTHONPATH=src .venv-platform/bin/python -m camera_platform backup \
  --state-dir outputs/camera-platform --output /absolute/path/new-backup.zip
PYTHONPATH=src .venv-platform/bin/python -m camera_platform restore \
  --archive /absolute/path/new-backup.zip --state-dir /absolute/path/new-restored-state
```

복원은 Worker를 시작하지 않으며 queued/running을 보존합니다. 이후 운영자가 Worker를 시작하면 기존 중단 작업을 failed로 기록하는 정상 복구 정책이 적용됩니다. Python 환경·코드·외부 데이터·장비 설정·암호화 키는 백업에 포함하지 않습니다. [백업·복원 절차](camera-platform-backup.md)를 확인하세요.

현재 에디션에는 PostgreSQL/Object Store adapter, SSO/RBAC, 최종 승인 서명, 외부 장비 제어, 일반 동적 3D·시야/거리별 PSF·HDR/LFM, 일반 영상 Surrogate와 전체 불확실도 전파가 없습니다. 제한된 정적 3D·분광 렌더링, AE/AWB 폐루프, 수치 지표 Surrogate와 입력 경계 전파는 각 버전의 근거와 적용 범위 안에서 구현되어 있습니다. 명시적 분광 영상의 공간·시간 적분, 프레임별 기록 조건 적용 및 RAW ISP 재처리는 위 범위에서 구현되어 있습니다. 상세 Solver 지원 범위는 [Lens Factory](camera-platform-lens-factory.md), [Meep Factory](camera-platform-meep-factory.md), [Prysm PSF Factory](camera-platform-psf-factory.md)에 명시합니다. [상용화 기준](camera-platform-commercialization.md)에 후속 작업과 완료 조건을 정리했습니다.

공통 API와 별도 실행 계층의 설계 참고: [FastAPI 모듈 구성](https://fastapi.tiangolo.com/tutorial/bigger-applications/), [계산 작업 분리 안내](https://fastapi.tiangolo.com/tutorial/background-tasks/). 물리 모델의 계약은 구현과 검증 근거에 따라 제한적으로 적용합니다.
