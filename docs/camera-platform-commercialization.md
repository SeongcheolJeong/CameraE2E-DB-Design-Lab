# Camera Platform 상용화 완료 기준과 개발 순서

**2026-09-27 현재 개발 소스는 `0.16.1.dev32`이며, 기본 작업실의 마지막
확인 실행 버전은 v0.15.0이다.** 소스 위치 광학을 시간·잔류 전하에 이어
독립 브래킷 HDR/LFM과 공유 전하 DCG에 연결했다. 소스 셀/서브셀 RI→PSF→
전하 생성→행별 노출→전기 RAW·ISP 경로의 계산·출처 검사를 확장하고,
HDR/DCG 정적 조건에서 서브셀 변화와 출처 라벨을 구분한다. 고급 ISP는
선택을 잠근 후 새 홀드아웃에서 평가하는 계약에 표시 영상 시간 변화 guard를
선택형으로 추가했다. [dev32 근거](../reports/camera_platform/physical-chain-v0161-dev32/evidence-summary.json)의
140개 HDR/DCG 교차, 43개 시간·잔류 전하·3D, 7개 등록 소스/ROI, 19개 ISP
시험은 중복 가능한 합성 계산과 로컬 실행 검사다. dev31의 보관 처방 응답 결합과
두 실제 3D 광원 PWM·트랩 경로는 [이전 근거](../reports/camera_platform/physical-chain-v0161-dev31/evidence-summary.json)에
남아 있다. 기본 작업실은 dev31 당시 Builder·Releases 읽기 전용 화면에서
연결 상태를 확인했고 저장 패키지 2건은 모두 `independent_validation_missing`으로
배포 차단을 표시했다. dev32 설치·화면을 새로 검증하지 않았다. 독립 실측은
0건이고 물리 정확도·연속 가림 오차·블라인드 ISP 평가·고객 설치/운영은
미검증이다. 개발 결과를 전체 상용 출시 완료율이나 실물 성능 보증으로
환산하지 않는다.

**이전 dev16 기록:** 당시 개발판 v0.16.0.dev16은 제한된 수치·API·실제 브라우저 범위를 검증했습니다. v0.15의 사례별 설계 검토와 기존 원본은 유지합니다. [잔류 이력 ISP 계약](camera-platform-retained-isp.md)은 실제 모델 등록·재처리·6후보 비교와 검증 범위를 설명합니다. 전체 기준 회귀와 마지막 좁은 API 수정의 집중 재검증을 구분하며, 수정 후 전체 회귀를 다시 실행했다고 주장하지 않습니다. 재시작·백업·정리의 당시 상태는 [dev16 근거 요약](../reports/camera_platform/retained-isp-v016/evidence-summary.json)을 따릅니다.

**이전 dev16 인도 계획:** 당시 우선순위는 광학·native 픽셀·비선형/DCG·잔류 전하·고급 ISP를 설치 가능한 v0.16으로 통합하는 것이었습니다. 설치된 코드와 정적 파일의 일치, 실제 설치 실행·Worker·재시작·기존 기록 보존을 확인한 뒤 전기 폐루프 HDR/LFM의 다음 확장을 제안했습니다. 기본 앱과 개발 소스의 기능 차이를 줄이는 출시 경계는 현재도 남아 있습니다. [당시 개발 진행 상태](../reports/camera_platform/progress-20260923.md)를 따르며, 개발 시험 수를 상용제품 완료율로 계산하지 않습니다. 독립 실측·깨끗한 고객 환경·배포 및 운영 Gate가 남아 있으므로 일곱 모듈·네 실행 방식의 전체 상용화 Goal은 미완료입니다.

**이전 v0.14.0 인도 기록입니다.** [처방 OPD·위치별 PSF](camera-platform-prescription-optics.md)를 기본 작업실에 연결했습니다. 같은 처방의 광량·입사각 응답과 PSF를 새 Lens/Camera로 등록하고 실제 64×64 RAW/RGB와 RAW 보존 ISP 재처리를 실행했습니다. [실행 근거](../reports/camera_platform/prescription-primary-delivery.json)와 [기본 화면 조회](../reports/camera_platform/prescription-ui/primary-v014/verification.json)를 확인합니다.

[백업·설치 전환](../reports/camera_platform/primary-prescription-upgrade.json)은 기존 **215개 기록·74개 작업·437개 감사 이벤트**를 보존했습니다. [기존 시간 촬영](../reports/camera_platform/primary-prescription-legacy-rerun.json)의 RAW/RGB가 동일하며 [최종 무결성](../reports/camera_platform/primary-prescription-integrity.json)을 확인했습니다. 원래 상용화 Goal은 계속 진행 중입니다. 실측은 **0건**, 총 예측 불확실도는 **unknown**이고 독립 고객 환경·일반 물리 모델·권한·Agent 흐름 등이 남아 있습니다. [최신 인도 기록](../reports/camera_platform/delivery.json)과 [요구사항 감사](camera-platform-requirements-audit.md)를 따릅니다.

아래 v0.13 및 이전 버전 설명은 각 기능의 도입 당시 근거입니다.

**이전 v0.13.0 인도 기록입니다.** [저장된 분광 복사휘도 재사용](camera-platform-scene-projection.md)을 기본 설치 작업실에 연결했습니다. 성공한 장면의 원본 `Lλ`를 보존하고, 같은 시점·초점거리·Native 격자·파장에서 새 투과율과 조리개로 센서면 광량을 계산한 뒤 대상 카메라의 RAW/RGB를 생성합니다. 조건이 달라지면 재렌더링을 요구하며, 원본 렌더링과 새 변환의 출처·환경을 별도 보존합니다.

[v0.12→v0.13 설치 전환](../reports/camera_platform/primary-projection-upgrade.json)은 백업을 만들고 기존 **212개 기록·71개 작업·425개 감사 이벤트**를 보존했습니다. [기본 작업실의 재사용 실행](../reports/camera_platform/primary-projection-verification.json) **9항목**, [기존 시간 RAW/RGB 재실행](../reports/camera_platform/primary-projection-legacy-rerun.json), [후속 무결성 검사](../reports/camera_platform/primary-projection-integrity.json)를 확인했습니다. 새 예제와 재실행은 **3개 기록·3개 작업·12개 감사 이벤트**를 추가했고 원래 기록은 유지했습니다. 기본 작업실의 [읽기 전용 화면 검사](camera-platform-installed-runtime.md#기본-작업실의-화면-검증)는 **7개 흐름·168항목**을 통과했습니다(POST 0회·브라우저 오류 0건).

`ready`와 계산 성공은 제한된 입력·실행 계약을 확인한 상태입니다. 실물 예측 정확도는 미확립이고 총 불확실도는 `unknown`이며, 같은 원본에서 재사용한 MC 표준오차는 공유 표본의 수치 진단입니다. 실제 장비 측정 데이터는 **0건**입니다. 독립 실측, 일반 광학/픽셀/전기 모델·HDR/LFM·고급 최적화·Agent·권한·고객 환경 운영을 포함한 **원문 일곱 모듈·네 실행 방식의 전체 Goal은 미완료**입니다. 설치 검증은 기존 계산 의존성을 공유하며 깨끗한 OS 설치를 보장하지 않습니다.

## v0.13 인도 상태와 상용 출시 판정

[저장 분광 복사휘도 재사용](camera-platform-scene-projection.md)은 같은 광선 격자에서 카메라 조합을 바꾸는 계산 비용을 줄이는 인도 추가분입니다. 원본 렌더링과 새 변환의 환경·출처를 분리하고, 실제 Lens·Sensor·ISP 계약을 만족할 때만 새 광량과 RAW/RGB를 생성합니다. 임의 장면·센서 구조를 정밀하게 예측하는 기능이나 전체 실행 Graph 최적화로 계산하지 않습니다.

[개발 회귀 1,216개](../reports/camera_platform/projection-development-tests.xml)·[소스/환경 결합](../reports/camera_platform/projection-development-test-binding.json), [별도 전환 78개](../reports/camera_platform/projection-runtime-transition-tests.xml)·[직접 입력 해시 결합](../reports/camera_platform/projection-runtime-transition-tests-binding.json), [Node 6항목](../reports/camera_platform/scene-projection-ui-state-regression.json)·[개발 화면 26항목](../reports/camera_platform/projection-ui/development/verification.json) 및 [Checkout 밖 패키지 실행·132파일 일치](../reports/camera_platform/release-archives/v0.13.0/package-verification.json)를 확인했습니다. 전환 시험 36개는 전체 회귀와 중복되며 42개가 추가분입니다.

[설치본 격리·재시작 17항목](../reports/camera_platform/projection-installed-runtime-verification.json)은 개발 Checkout 변경과 재시작 후 같은 원본·새 광량·MCSE·RAW/RGB를 확인했습니다. 별도 설치 QA도 [광휘 재사용 26항목](../reports/camera_platform/projection-ui/installed-v013/verification.json)·[카메라 준비 20항목](../reports/camera_platform/projection-ui/preparation-v013/verification.json)·[장면 작성 36항목](../reports/camera_platform/projection-ui/authoring-v013/verification.json)을 통과했습니다. 기본 작업실은 위 백업·보존·실제 계산 증거에 따라 v0.13으로 전환했고 읽기 전용 화면 7개 흐름·168항목도 통과했습니다. G5/G6의 독립 실측·총 불확실도, 고객 환경 운영·권한·배포 승인과 전체 Goal은 미완료입니다.

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

**v0.10.0 인도 전 개발 검증 기록:** [폐루프 공통 관측·비교](camera-platform-control-observation.md)의
Test→실제 RAW→관측 업로드→정합/잔차→근거 보존을 별도 개발 작업실에서 검증했습니다.
기본 설치본 v0.9.0과 구분하며, 이 추가분만으로 G5/G6의 실물·총 불확실도 또는 상용 출시
Gate가 충족되지는 않습니다. 후속으로 [취득 원본과 분석 Revision](camera-platform-capture-analysis.md),
공통 Test 작성·재분석 화면을 연결했습니다. 회귀 시험 1,082개, 재분석 HTTP/Worker 5항목과
브라우저 21항목을 확인했습니다. 응답 RAW/원문 로그 업로드 화면과 새 패키지 인도는 남았습니다.

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

**v0.5.0 당시 기반 기록:** 플랫폼은 실행 가능한 로컬 엔지니어링 기반으로 시작했습니다. 제한된 광학 특성 계산,
모델 등록·조합, Native 분광 평면 영상과 PSF의 실제 RAW/RGB 계산, 보존 RAW의 변경 ISP 처리,
RAW/Dark 분석·제한 보정 후보·비교·근거 ZIP·백업 복구를 연결했습니다.
기존 pyisetcam 배포 버전과는 별도로 관리합니다.
이 문서의 향후 성능·운영 수치는 **제안하는 합격 목표**이며, 이미 측정하거나 달성한
결과가 아닙니다. 상용 출시 완료나 특정 카메라의 예측 정확도를 주장하지 않습니다.

v0.5에는 시각별 분광 영상 적분, Rolling/Global shutter, 프레임별 기록된 노출·Gain·ISP
적용과 시간 RAW Replay가 추가됐습니다. 아래 v0.4 상세표는 이전 기반의 범위를 남긴
것이며 시간 기능의 현재 범위는 [시간 Runtime](camera-platform-temporal.md)과
[요구사항 감사의 v0.5 추가분](camera-platform-requirements-audit.md)을 우선합니다.
또한 [설치 Runtime](camera-platform-installed-runtime.md)으로 기본 작업실을 개발 소스와
분리하고 실제 계산·재시작·원본 보존을 확인했습니다. 공유 의존성 환경을 재사용한
동일 버전 전환이며 G1/G12의 깨끗한 설치·다른 버전 Migration/복귀를 완료한 것은 아닙니다.

첫 개발·검증 사례는 Lens 교체로 생기는 주변부 RAW 색 비율 변화입니다. 사용자 목표는
일곱 모듈과 네 Runtime을 갖춘 통합 플랫폼이며 이 사례로 범위를 축소하지 않습니다.
일반 3D Scene·실제 Lens/시야별 PSF·시간·Surrogate·최적화·Agent 요구와 인도 조건은
[원문 요구사항 감사](camera-platform-requirements-audit.md)에서 별도로 추적합니다.
네 방식 중 Quick Estimate와 Response-DB의 제한 정적/공간 경로, Detailed Characterization의
세 제한 Adapter에 더해 v0.8의 수치 지표 Surrogate 학습·추론·별도 조건 검증이 구현되어 있습니다. 일반 응답/영상 Surrogate와 연속 영역의 오차 보장은 남아 있습니다.
현재 개발 사례나 출시 Pilot을 전체 목표의 범위 축소로 취급하지 않습니다.

## v0.4.0에서 실행되는 것

| 영역 | 현재 산출물 | 현재 한계 |
| --- | --- | --- |
| Registry | 불변 자산/모델/시험, 측정 JSON·RAW/Dark·분광 이상 영상 NPY 원문, PSF·ISP 프로필과 출처·해시·보정 계보 | 출처 진위 인증, 장비 연결, 일반 3D 자산·대용량 저장·접근 권한 미구현 |
| Composer | 형식·조건·파장/각도·센서 크기·중복 효과 검사, PSF 부모 범위/정확한 파장/F-number·원본 RAW와 ISP 형식 검사 | 선언하지 않은 물리 효과·기구 조건·PSF와 각도 응답의 물리적 결합까지 보증하지 않음 |
| Detailed Factory | RayOptics 분광 RI/각도, Meep signed R/T/A, Prysm 선언 동공의 Pixel 적분 PSF·복소 Kernel OTF·독립 수치 검사 | 실제 Lens Wavefront 추출·시야별 PSF·일반 Pixel 구조·전하 수집 QE 미지원; 물리 정확도 미확립 |
| Camera Runtime | Quick Estimate·정적 Shading, 추가 `spatial_scene_v1`의 Native 분광 이상 영상→PSF→RI/각도 QE→실제 RAW·튜닝 ISP | 입력은 3D 렌더링이 아닌 센서 이상 영상면의 광량; 공간 불변 PSF와 각도 응답의 분리 근사, 깊이/가림·시야별 왜곡·움직임/HDR 미지원 |
| ISP Replay | 등록 Capture/완료 Run의 동일 RAW→명시적 Float64 BLC/Bilinear/고정 WB/CCM/Tone, 전체 프레임·중간 배열·취득 조건과 원본 해시 보존 | 임의 Graph·LSC/고급 ISP·Fixed-point/HW Backend·동적 제어 미지원; 재촬영·물리 색 정확도 검증이 아님 |
| RAW 관측 | 선형 RGGB NPY Import, Native crop/CFA/ROI와 Dark 보정 고정, 동일 연산자의 예측값, 원본 재분석 확인 | 벤더 RAW 디코더·장비 제어 없음; ADC 검사만으로 Full-well 선형성 보증 불가 |
| Calibration | R/B 각도응답 배율의 제한된 후보 피팅, 식별성·QE 범위 검사, 부모·훈련 계보 보존 | 일반 모델 피팅·자동 승인 없음; 훈련 잔차는 예측 불확실도가 아님 |
| Validation | 관측 의미 정합, 조건·불확실도·독립 데이터 판정, 잔차 | 예측 불확실도는 unknown; 현재 Runtime로 실물 검증 PASS 불가 |
| 실행/보존 | SQLite 작업 큐, 취소·시간 제한, 해시·감사 검증, 일관된 Snapshot과 새 위치 복구 | 단일 호스트/Worker; 운영 복구 Drill·부하·Migration·배포 업그레이드 검증 필요 |
| 실험/Release | 조합·노출 Sweep, 보정·부모·훈련 자료·Scene·Factory·원본 RAW·변경 ISP를 연결한 근거 Manifest/ZIP와 차단 사유 | 일반 민감도·다목적 최적화·적응형 DB·Agent, 사용자별 승인, 서명된 배포/실행 환경 복구 Package 없음 |

v0.4의 직접 구현 근거는 [PSF 독립 검증](../tests/platform/test_psf_factory.py),
[공간 Runtime 검증](../tests/platform/test_spatial_runtime.py),
[ISP 재처리 검증](../tests/platform/test_isp_replay.py),
[실제 Prysm→Native Scene→RAW/RGB→변경 ISP→원본 근거 ZIP 시험](../tests/platform/test_spatial_workflow.py)입니다.
현재 v0.4 [전체 회귀](../reports/camera_platform/tests.xml) 674개 시험이 실패·오류·Skip 없이
통과했습니다. 실제 HTTP 보고서도 [기존 Camera·RAW 관측·보정·복구](../reports/camera_platform/http-worker-verification.json)
10항목, [RayOptics/Meep Factory](../reports/camera_platform/factory-http-verification.json) 5항목,
[Prysm→Native Scene→RAW/RGB→변경 ISP](../reports/camera_platform/imaging-http-verification.json) 6항목이 통과했습니다.
[공간·PSF·Replay UI](../reports/camera_platform/spatial-ui/headless-qa.json) 60항목은 실제 연산,
동일한 원본 RAW와 변경 RGB, 근거 ZIP을 확인한 화면 크기 조정 전의 기능 증거입니다.
이 브라우저 흐름은 완료 Camera Run에서 Replay를 실행했으며 Capture 원본의 브라우저
흐름까지 확인한 기록은 아닙니다. 최신 배포 화면의 상태는 해당 최신 보고서를 확인합니다.

[설치된 Wheel 검증](../reports/camera_platform/package-verification.json)은 Checkout 밖
`python -I`에서 실제 세 Solver·Native 공간 계산·Replay와 정적 파일·Artifact 해시를 확인했습니다.
기존 의존성을 재사용했으므로 깨끗한 OS/Dependency-resolution 시험을 대신하지 않습니다.
이전 관측/보정 UI 22항목과 RayOptics/Meep Factory UI 112항목은 당시 화면의 역사적 증거이며,
현재 버전으로 다시 실행한 위 HTTP·패키지 보고서와 구분합니다. 보고서의 버전·소스 해시와
맞는 결과만 인용하며, 이 통과 수는 물리 검증·지속 운영·출시 승인 수가 아닙니다.

기본 자산은 **가정한 해석식 기반 Engineering Fixture**입니다. 실물 측정값이나
특정 벤더 제품의 검증된 응답을 대신하지 않습니다. JSON의 `license` 필드나
`source_kind="measured"` 선언도 실제 사용권과 계측 신뢰도를 자동 보장하지 않습니다.

현재 `/export`는 JSON Manifest이고 `/bundle`은 원본 Artifact를 담은 Evidence
ZIP입니다. 공간 계산과 Replay의 근거에도 원본 분광 영상·PSF Factory·부모 모델·RAW와
선택 ISP 프로필을 연결합니다. Release ZIP에는 연결 Validation에서 참조한 등록 Measurement의 원문
바이트와 RAW/Dark 원본, 보정 후보의 보고서·부모 모델·훈련 시험/측정/취득 근거도
포함됩니다. 포함 파일의 해시를 확인하고, 압축 전 Artifact 합계를
256 MiB로 제한하며, HTTP 전송 뒤 임시 ZIP을 정리합니다. 이 기능은 오프라인 근거
검토를 지원하며 실행 환경·소스·서명된 배포 또는 최종 승인은 포함하지 않습니다.

별도 로컬 CLI로 Registry와 참조 Blob의 일관된 Snapshot 및 새 위치 복구를 지원합니다.
Schema·감사 이력·작업 상태·원본 해시를 확인한 뒤 복구 디렉터리를 공개하며, 기존
디렉터리를 덮어쓰거나 Worker를 자동 시작하지 않습니다. Archive와 전체 압축 해제는
각각 16 GiB, 개별 파일은 4 GiB 제한입니다. 암호화·서명·Schema Migration·실행 환경
복구는 없으므로 G7/G12의 실제 운영 Drill과 배포 검증은 여전히 필요합니다.

## 우선순위와 단계별 인도 조건

각 단계의 승인은 필요한 선행 증거가 확인되면 진행합니다. 독립적인 소프트웨어 작업은
실측 확보와 병행합니다. 일정은 담당자·계측 장비·원본 특성 데이터와 검증 범위를 확인한 뒤 산정합니다.

| 순서 | 단계 | 구현/데이터 작업 | 다음 단계로 넘어갈 근거 |
| --- | --- | --- | --- |
| 0 | 로컬 에디션 재현 | 설치/기동/Worker/오류 복구, API·Runtime·Scene/PSF·RAW/ISP·Calibration 계약, 예제·한계 문구 | 깨끗한 환경에서 A/B·공간 계산, 동일 RAW의 ISP 변경, 원본/조건·Export·백업 복구 재현 |
| 1 | 실물 Shading Pilot | 승인된 시험, Lens A/B·Sensor 응답, 실제 RAW/Dark·계측 근거를 구현된 공통 관측 Adapter에 연결 | 동일 시험에서 계산과 독립 실측의 비교가 유효하고 오차 원인을 단계별로 추적 |
| 2 | 검증된 모델 버전 | 구현된 제한 피팅의 실측 적용, 보정/Holdout 분리, 불확실도 산출/전파, 독립 평가 | 정해진 적용 범위에서 사전 등록한 특성·오차·불확실도 기준 충족 |
| 3 | 공간·동특성·모델 생성 확장 | 구현된 Lens/선언 PSF·평면층 FDTD에서 실제 Wavefront·시야/거리별 PSF·3D/재질/광원·Pixel/전하 수집으로 확장; 시간/AE/AWB/HDR/LFM·일반 ISP와 관측 연결 | 새 모델·조건이 실제 RAW/RGB에 반영되고 단위·적용 범위·수렴성·독립 기준 보존; 광흡수/전자 QE와 재처리/재촬영 구분 |
| 3A | 실험 자동화와 Surrogate | 일반 민감도/공차·다목적 Trade-off·적응형 추가 해석/실측, 학습 계보/오차/범위가 있는 Surrogate, Agent 계획·비용·도구 정책 | Holdout·범위 이탈·비용 제약·재현성 검증, 조건별 불확실도에 근거한 추가 실험 선택 |
| 4 | 팀 운영 Pilot | 사용자/역할/프로젝트 경계, 저장 Backend 분리, 실제 백업 복구 Drill·Migration, 자원 관리 | 권한·복구·취소·동시 실행 시나리오와 명시한 성능 목표를 통과 |
| 5 | 첫 상용 배포 검토 | 배포권 Audit, 설치 Package, 지원 정책, Release 승인/철회, 결과 설명서 | 모든 출시 차단 조건 해소 및 담당 검토자의 기록된 승인 |

1–2단계가 특정 Pilot의 물리 근거를 만듭니다. 3/3A단계의 공간·시간·일반 ISP·자동화와
팀 운영은 전체 사용자 목표에 포함되며 병행할 수 있습니다. Solver나 화면의 수,
직접 구현 시험만으로 실제 물리 예측 정확도·운영 Gate를 대신하지 않습니다.

## 첫 실물 Pilot의 고정 범위

시작 시 고객 또는 내부 시험 책임자가 다음 항목을 한 Test Specification으로
고정합니다.

- 대상: 특정 Lens A/B, Sensor 모델/모드 및 실물 Serial, 온도·조리개·초점.
- 입력: 균일한 광원의 측정된 분광 분포, 센서면 또는 명확히 변환한 기준면의 광량.
- 취득: RAW Packing/CFA/Bit depth/Black level, 실제 적용 Exposure/Gain,
  반복 프레임 수, 장비 ID·교정 버전과 불확실도.
- 관측: Dark/Black 보정 방식, Field 위치/ROI, R/G·B/G 계산법, 포화 제외 기준,
  분석 코드 버전과 원본 RAW 연결.
- 판정: 특성별 허용 오차, 불확실도 표현, 적용 범위 및 어떤 경우에
  `INCONCLUSIVE` 또는 `OUT_OF_DOMAIN`으로 남길지.

초기 작업량 계획으로 2개 Lens × 2개 광원 × 3개 비포화 노출 × 3개 Sensor Serial의
조합을 검토할 수 있습니다. 각 조합에서 중심과 4개 이상의 Field 위치를 취득하고
프레임 반복 수는 계측 불확실도 목표로 결정합니다. **이 수량 자체는 통계적 신뢰수준이나
Lot 전체를 대표한다는 보증이 아닙니다.** 필요한 반복 수와 샘플 수는 예비시험의
분산·편차와 제품의 주장 범위에 따라 조정합니다.

보정과 검증에 사용한 Dataset ID는 분리합니다. 적어도 하나의 실물 Serial 및
일부 조명 조건은 최종 모델 선택 전에 Holdout으로 고정하며, 그 데이터로 다시
파라미터를 맞춘 경우 새 독립 검증 데이터가 필요합니다. 보정 모델을 새 버전으로
등록하고 원본 측정값과 기존 모델은 보존합니다.

보정에는 등록된 원본 RAW 프레임의 내용 해시도 상속합니다. 같은 파일의 재인코딩이나
일부 프레임을 새 ID로 제출하여 독립성을 얻지 못하게 합니다. 픽셀을 변경한 파생 자료나
공유 Dark/장비 교정에서 생기는 상관관계까지 자동 판별하지 않으므로 실제 취득 계보와
계측 계획은 별도로 검토해야 합니다.

정적 Shading의 대표 Raster 채널 비율과 실제 센서 ROI 관측을 직접 같은 축으로 취급하지
않습니다. 구현된 **공통 관측 Adapter**가 Native pixel 위치→정규화된 Field 좌표,
CFA별 ROI 평균, Black/Dark 보정과 포화 제외 절차를 고정하고 예측에도 같은 연산자를
적용합니다. 지원 입력은 명시한 선형 RGGB 정수 NPY이며 RAW/Dark 합계 64 MiB 이내입니다.
현재 통합 보정에는 일치하는 Dark 프레임과 실측 선형성에 대한 외부 근거가 필요합니다.
실측 ROI의 R/G와 수식의 연속 기대값 사이에는 공간 표본·ADC 양자화·유한 프레임 수의
차이가 남을 수 있으므로 이를 불확실도 또는 비교 모델에 포함합니다.

새 공간 Runtime은 별도의 Native 평면 영상/PSF 계약을 사용합니다. 공간 출력과 재처리
RGB가 존재한다는 사실만으로 실제 Chart·PSF·색오차·SFR 관측 Adapter가 완성된 것은
아닙니다. 같은 RAW를 다시 처리해도 훈련·검증 독립성과 실제 취득 조건은 바뀌지 않습니다.

## 필수 출시 Gate

아래 기준은 담당자가 결과와 근거 위치를 기록해야 하는 Release Checklist입니다.
“지원함”과 “검증함”을 구분합니다. 합의된 출시 범위 밖의 항목은 미지원 또는 차단 사유로
남기되, 전체 플랫폼 목표에서 삭제하지 않습니다.

| Gate | 제안하는 측정 가능한 합격 기준 | 남겨야 할 증거 |
| --- | --- | --- |
| G1 재현 가능 설치 | 선언한 OS/Python 환경의 새 설치에서 샘플 등록→A/B 실행→Artifact 검증 성공; 문서에 없는 수동 수정 0건 | 정확한 Dependency Lock/Build ID, 설치 Log, 환경별 결과 |
| G2 물리 입력 계약 | 모든 출시 Model의 축·단위·기준면·효과·적용 범위 필수 항목 100% 존재; 범위 초과/중복 효과/잘못된 RAW 입력은 실행 전 거부 | 정상·경계·부정 입력 Test Report, Model 계약 버전 |
| G3 구현 검증 | 비포화 광량/노출/면적 선형성, 알려진 적분값·Airy/Delta PSF·경계 에너지·OTF 위상, 포화/Black/Seed와 Replay RAW 보존 불변식; 같은 환경 100회 반복 Artifact Hash 일치 | 독립 수식 기준값, 수치/공간/ISP Test Report, Run/Artifact 목록 |
| G4 실제 출력 연결 | Exposure/Gain/각도 응답/PSF가 기대 전자·실제 RAW/RGB에 반영되고, 고정 RAW의 WB/CCM 변경은 RGB에만 반영됨; 미연결 옵션을 지원으로 표시한 사례 0건 | 단계별 배열·원본/조건 비교, 실제 API/Worker/UI 흐름과 Capability Report |
| G5 실물 모델 검증 | 출시 범위의 모든 사전 등록 Holdout 셀에서 관측 정합, 독립성, 예측·측정 불확실도 충족; 선언한 특성 기준에 FAIL/미해결 INCONCLUSIVE 0건 | 원본 RAW·장비 교정·분석 버전·Calibration/Holdout 구분·Validation Report |
| G6 불확실도 | 출시 특성마다 모델 오차·계측 오차·보간/표본화 영향의 정량 표현 및 출력까지의 전파 구현; 누락을 0으로 처리한 사례 0건 | 불확실도 방법서, 범위별 검증 결과, 민감도 또는 전파 검증 |
| G7 데이터 보존/복구 | Metadata와 Blob이 일관된 Backup에서 새 위치로 복구; 등록된 원본/Artifact Hash 100% 일치; 중단 작업은 명확한 상태로 복구 | 실제 복구 Drill, Hash 비교, 실패·복구 시각 |
| G8 실행 안정성 | 100개 순차 Pilot Run에서 유실/잘못된 성공 상태 0건; Timeout/취소/Worker 강제 종료/재시작 시험 모두 통과 | 작업 상태 이력, Child 종료 확인, 중복 실행 분석 |
| G9 성능 | 선언한 기준 Host에서 64×64·4 frame Response-DB 계산 p95 ≤10초, 실행 중 주요 API p95 ≤2초를 우선 목표로 측정 | Host/전력 모드/Cold vs Warm 분리, 표본 수, 시간·메모리 측정값 |
| G10 권한 | 출시 역할의 모든 API·Artifact·Export 경로에 접근 Matrix 시험; 타 프로젝트 원본 열람/최종 승인 우회 0건 | Threat Model, 역할별 긍정/부정 테스트, 접근 Log |
| G11 라이선스/배포권 | 배포 엔진·전이 의존성·모델 원본·생성 결과의 권리 목록에서 미해결 항목 0건 또는 해당 항목을 배포 범위에서 제거 | SBOM, 출처·라이선스 원문, 벤더/데이터 허가 근거, 검토 기록 |
| G12 Release와 지원 | 모델/시험/분석 코드/환경/원본 근거를 고정한 Package 검증 및 이전 버전 복귀 Drill 성공; 지원 범위와 알려진 한계 명시 | Release Manifest, 포함 Blob, 승인 이력, Rollback 결과, 사용자 문서 |

G9의 64×64 Shading 수치만으로 PSF Factory·Native 분광 Scene·전체 프레임 ISP의 성능을
인정하지 않습니다. 이 경로에는 파장 수·동공/수신 격자·Crop 크기·프레임 수·중간 배열
보존을 고정한 별도 시간·메모리 목표가 필요합니다. 현재 자원 상한이나 한 번의 직접 실행
시간은 지속 성능 검증이 아닙니다.

G9 수치는 고객 요구와 목표 하드웨어가 확정되면 갱신하되, 결과를 본 뒤 임의로 기준을
바꾸지 않도록 Test Specification을 버전 관리합니다. 다중 사용자 에디션에는 별도의
동시 요청/Queue/대용량 Artifact 부하 시험이 필요합니다. 현재 Local Edition의
실행 제한은 성능 합격 결과가 아닙니다.

G5의 합격 범위를 좁혀 출시할 수는 있습니다. 예를 들어 25°C·고정 조리개·정적
균일광 조건만 근거가 충분하다면 그 범위만 승인하고 고온/HDR/주행 장면은 미지원으로
표시합니다. 특정 조건의 PASS를 다른 온도·입사각·광원·Lot으로 자동 확장하지 않습니다.

## 불확실도와 Calibration 구현 순서

현재 제한된 각도응답 보정 후보 생성은 구현되어 있습니다. Green·정상 입사 QE·Lens·
광원·Readout을 고정하고 비영 입사각 노드의 R/B 배율만 추정하며, Rank·조건수·QE 상한을
검사합니다. 실측은 동일 Serial·온도와 Native geometry·외부 선형성 근거를 요구하고,
Synthetic 데이터는 구현 시험용으로 구분합니다. 후보는 새 모델로 보존되며 자동 승인하지
않습니다. 예측 불확실도는 여전히 unknown이므로 다음 근거와 계산 경로가 필요합니다.

1. 실측 원본의 불확실도와 반복 프레임/ROI 분석 방법을 등록합니다. 단순 숫자 오차와
   수치 모델의 불확실도를 구분합니다.
2. 현재 제한된 R/B 각도응답 배율이 Pilot의 오차 원인을 설명할 수 있는지 확인합니다.
   다른 파라미터를 추가할 때는 범위와 식별성을 다시 검토하고 전자/RAW 단계의 잔차를 봅니다.
3. 등록된 Calibration 자료로 후보를 만들고 적용 범위별 오차를 설명합니다. 훈련 잔차를
   불확실도 경계로 대신하지 않고, 모델 불확실도를 분광·각도 적분과 RAW 관측까지 전파합니다.
4. 모델 선택에 사용하지 않은 데이터로 기준을 확인합니다. 기존 지원 조건이 악화된
   경우 이를 Regression으로 남기고 자동 승인하지 않습니다.
5. 검토된 결과만 새 승인 후보에 연결합니다. 일반 Run 실행 API와 최종 승인 권한을
   분리한 후 모델의 배포 상태를 변경합니다.

일반 파라미터 Calibration, 표준별 측정 분석, 모델 불확실도 전파는 현재 기능에
포함되어 있지 않습니다. 제한 피팅의 잔차가 작거나 곡선이 일치해도 독립 실측 검증을
대신하지 않습니다. 입력 계약과 반환 상태는 [Calibration 문서](camera-platform-calibration.md)에 있습니다.

## 구현된 Solver와 다음 확장

RayOptics·Meep·Prysm은 제한된 입력으로 실제 실행됩니다. 다음 연결은 도구 이름보다
현재 시험에서 부족한 물리 응답을 기준으로 정합니다. 특히 독립 선언 동공 PSF와 실제
Lens 처방의 Wavefront, 평면층 광흡수와 Pixel 전하 수집의 경계를 유지합니다.

| Adapter | 현재 산출물과 다음 연결 | 검증에서 확인할 사항 |
| --- | --- | --- |
| Lens 해석 | RayOptics 구면/평면 처방의 분광 입사각/RI를 실제 소비; 다음은 일반 처방·재질·Wavefront·시야/거리별 응답 | Ray weight·단위·부모 적용 범위, 독립 동일 조건 비교 |
| PSF/물리광학 | Prysm 선언 원형 동공의 픽셀 적분 PSF와 복소 Kernel OTF를 공간 Runtime에서 소비; 다음은 실제 동공·Field 의존·벡터 모델 | Airy·수신 Bin 에너지·독립 격자 수렴, 유한 꼬리 재정규화, Pixel aperture 중복 방지, Kernel OTF/순수 Lens MTF/SFR 구분 |
| Pixel FDTD | Meep 수직 입사 평면층 signed R/T/A; 다음은 명시적 Pixel 구조·광학/전기 결합 | Mesh/PML/시간·보존량, 재질 출처, 광흡수와 수집전자 QE의 차이 |
| 장비/벤더 RAW 연결 | 장비별 취득·원본 디코딩→구현된 공통 선형 RGGB 관측 입력 | Packing·Black·CFA·Gain·ROI 의미와 독립 기준값 |

외부 도구가 실행된 사실과 실제 Sensor 구조를 재현했다는 주장은 다릅니다. 구조나
재료가 가정이라면 결과는 설계 탐색 모델로 분류합니다. 도구·데이터의 상용 사용권은
Adapter 개발과 병행하여 검토하며 이 문서는 법적 허용 여부를 판정하지 않습니다.

## 팀 운영과 배포 경계

현재 Loopback Token은 로컬 웹 요청 보호입니다. 사용자 신원, SSO, RBAC,
Project/Tenant 격리, 고객 데이터 접근 정책을 제공하지 않습니다. 네트워크에
공개하는 에디션은 다음 역할 경계를 먼저 구현해야 합니다.

| 역할 | 허용할 작업 예 | 별도 권한이 필요한 작업 |
| --- | --- | --- |
| Viewer | 허용된 모델·실행·보고서 조회 | 원본 Export, 고객/벤더 제한 데이터 접근 |
| Engineer | 후보 모델/시험 등록, 허용 비용 내 계산, 비교 | 원본 삭제·수정, 최종 승인, 기준값 교체 |
| Measurement operator | 승인 시험의 원본 데이터 취득·등록 | 계측 교정 상태 변경, 모델 최종 승인 |
| Reviewer / Release approver | 독립 근거 검토, 승인/철회 | 자신이 작성한 모델의 단독 최종 승인 여부는 정책으로 제한 |
| Operator | Backup/복구, Worker/저장소 운영 | 데이터 과학적 승인과 정책 우회 |
| Agent | 명시된 사용자의 범위에서 같은 API 실행 | 원본 변경·승인 정책 변경·최종 승인 자동 획득 |

PostgreSQL, Object Store, Xarray/Zarr를 추가하는 것만으로 팀 운영이 완성되지는
않습니다. Metadata/Job/Blob 저장 인터페이스, 트랜잭션, Lease, 접근 정책, Blob
보존/정리 정책, Migration과 복구를 같이 구현해야 합니다. 구체적인 코드 경계는
[아키텍처 문서](camera-platform-architecture.md)에 정리했습니다.

현재 백업·복구 CLI는 단일 호스트 데이터 보존의 기반입니다. 복구된 작업 상태를
보존하고 Worker는 자동 시작하지 않습니다. 운영자는 원본·작업 상태를 확인한 뒤
기동해야 하며, 팀 에디션의 권한·보존 기간·암호화·장애 전환·Schema Migration과 실제
복구 훈련을 별도로 검증해야 합니다.

## 라이선스·출처와 제품 문구

현재 프로젝트 또는 Fixture에 적힌 MIT 표시는 모든 기존/신규 의존성, ISETCam
자료, 벤더 특성표, 렌즈 처방, 측정 장비 데이터 및 생성 결과의 상용 재배포권이
정리되었다는 뜻이 아닙니다. 출시 전에 배포 대상별 라이선스 Audit가 필요하며
현재 상태는 **미완료**입니다. 제한된 자료는 내부 조회 전용·별도 제공·제외 등의
배포 방식을 검토하고 근거를 남깁니다.

고객에게 보여줄 상태는 다음처럼 구체적으로 표현합니다.

- “이 모델은 지정한 파장·입사각·온도 범위에서 이 Dataset으로 검증됨.”
- “현재 계산은 가정한 응답으로 실행되었으며 물리적 예측 불확실도는 미확정.”
- “현재 공간 계산은 Native 분광 평면 영상과 선언된 PSF를 사용하며 3D·움직임·HDR는 포함하지 않음.”
- “이 결과는 보존된 RAW에 WB/CCM을 바꿔 적용한 것이며 센서·노출을 바꾼 재촬영이 아님.”
- “이 OTF에는 픽셀 적분과 유한 Kernel 정규화가 포함되며 순수 Lens MTF나 실측 SFR가 아님.”

정확도나 표준 적합성 수치를 근거 없이 붙이지 않습니다. 출시 범위·검증 근거·미해결
조건을 같은 Release Package에서 확인할 수 있어야 합니다.


## v0.5 시간 촬영 추가에 따른 출시 상태

기록된 프레임 설정을 적용하는 시간 Runtime과 UI가 추가되었다. 선형/ZOH 장면 적분,
Native 행별 Rolling/Global exposure, 실제 RAW/RGB 및 프레임별 원본 유지·재처리,
Clock/순서/유효 범위 검사는 구현 증거를 갖는다. 센서 시간 변화를 프레임 분산 SNR로
계산하지 않는다. 선언한 동공 모델은 새 후보의 명시적 선택으로만 노출/Gain 고정을
제외할 수 있고, 부모 Lens·Sensor·ISP 유효 범위와 PSF 조리개 선언은 유지한다.

이 기능은 G2–G4 및 G7의 검증 범위를 넓힌다. G5–G6 실물·불확실도, 폐루프 AE/AWB·HDR,
3D Scene·일반 렌즈/Pixel 모델, 권한·설치·지속 부하·배포권 Gate는 여전히 미완료다.
개별 알고리즘 시험·HTTP/Worker·브라우저·설치 Package 증거를 별도로 기록하며
한 번의 시간 실행 성공을 전체 실물 카메라의 시간 정확도 검증으로 인정하지 않는다.
