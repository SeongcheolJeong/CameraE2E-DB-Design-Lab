# 원문 요구사항 대비 Camera Platform 감사

**현재 읽기 순서 (2026-09-27):** 아래 내용은 작성 당시의 설치·개발 감사
이력이다. 현재 우선 개발 소스는 `0.16.1.dev32`이며
[dev32 근거](../reports/camera_platform/physical-chain-v0161-dev32/evidence-summary.json)와
[상용화 경계](camera-platform-commercialization.md)를 먼저 확인한다. 아래
v0.15 당시의 `55–60%`는 과거 정성 추정이며 dev32의 승인된 완료율이 아니다.
독립 실측 정확도·설치 전환·고객 출시 검증의 분모와 가중치가 정해지지 않아
현재 상용화 단일 %는 산정하지 않는다.

**2026-09-23 개발 소스 v0.16.0.dev16의 제한된 수치·API·브라우저 검증:** [잔류 이력 ISP](camera-platform-retained-isp.md)는 단일/HCG/LCG 중 한 읽기 경로의 전체 프레임을 고정하고, 같은 Bayer 위치의 공간 필터·인과적 시간 필터·제한된 상세 대비를 비교합니다. 고정 색 처리의 필터 bypass 기준에 대한 프레임별 왜곡과 시간 변화 왜곡을 보존하며, 최악 프레임 제약과 제한된 후보의 Pareto 비교를 제공합니다. 동일 RAW 출처·불변 모델·행별 시각·실제 중간 배열을 연결하는 ④ Runtime과 ⑥ Experiment & Optimization의 제한된 확장입니다. 센서 전하 역산·지연 보정·물리 SNR·일반 움직임 보정·적응형 전역 최적화는 완성하지 않았습니다. 기본 설치본 v0.15와 독립 실측 0건은 유지합니다.

[회귀 요약](../reports/camera_platform/retained-isp-v016/regression-summary.json)은 전체 기준 실행 4,108개 중 4,107개 통과와 오래된 OpenAPI 기대값 1건 수정을 구분합니다. 마지막 `black_level_dn`의 유한 int/float 동등성 수정은 53개 집중 시험으로 확인했으며, 중복을 제외한 최종 확인 범위는 Python 4,118개·미해결 실패 0개, Node 193개입니다. 마지막 좁은 제품 수정 후 전체 회귀를 재실행하지 않았습니다. 실제 CUA 브라우저에서 모델 등록·재처리·6후보 비교를 완료했고, 별도 [GET-only 근거](../reports/camera_platform/retained-isp-v016/browser-verification.json)는 2,544항목·GET 106회·POST 0회로 6개 기록·93개 Artifact·2개 ZIP을 확인했습니다. 이 수를 물리 검증 수나 요구사항 완료율로 환산하지 않습니다.

재시작·보존·정리의 최종 판정은 [dev16 근거 요약](../reports/camera_platform/retained-isp-v016/evidence-summary.json)을 따릅니다. 다음 우선순위는 dev16까지의 광학·픽셀·전기·ISP를 설치 가능한 v0.16으로 통합하고 설치 실행·재시작·기존 기록 보존을 확인하는 것입니다. 이후 전기 폐루프 HDR/LFM 확장을 제안하며, 원문 일곱 모듈·네 실행 방식의 전체 상용화 Goal은 계속 미완료입니다.


**2026-09-21 개발 소스 v0.16.0.dev5:** 사용자 우선순위에 따라 [Native 네 사이트·방위각](camera-platform-native-pixels.md), [공유 전하 HCG/LCG 전기 모델·RAW 합성](camera-platform-dcg.md)에 이어 [보존 Native RAW의 LSC·잡음 제거·선명도 비교](camera-platform-advanced-isp.md)를 확장했습니다. ISP v2는 불변 보정 버전, 단계별 배열·유효 마스크와 동일 RAW의 v1 기준 영상을 보존합니다. 별도 합성 반복 영상의 잡음·편향·질감·halo 비교는 계산 검증이며 실측 화질 개선이나 자동 최적값을 뜻하지 않습니다. 일반 회로/LOFIC·움직임 HDR/LFM·시간 ISP·전역 최적화·독립 실물 검증은 남아 있습니다. 기본 설치본 v0.15는 변경하지 않았습니다. 세부 검증 범위는 [DCG 근거](../reports/camera_platform/dcg-v016/evidence-summary.json)와 [고급 ISP 근거](../reports/camera_platform/advanced-isp-v016/evidence-summary.json)를 따릅니다.

**현재 기본 설치본은 v0.15.0입니다.** [사례별 설계 검토](camera-platform-cases.md)의 저장 API·SDK·화면을 기본 작업실에 연결했습니다. 질문·요구사항·고정 시험과 카메라·선택 근거의 결과 전체 해시·판단 이유·남은 문제·다음 계획을 불변 리뷰로 보존합니다. 근거가 없거나 손상된 사례는 사용 불가로 표시하고 정상 사례와 작업실의 조회는 유지합니다. [기본 설치 전환](../reports/camera_platform/cases-v015/primary-upgrade.json)은 **223개 기록·79개 작업·460개 감사 이력을 보존했으며 추가 행은 0개**입니다. 기본 사례 목록은 아직 비어 있고 QA 데이터는 이관하지 않았습니다. Agent 자동 실행·역할별 승인·실물 검증은 남아 있습니다.

[기본 화면 읽기 전용 5항목](../reports/camera_platform/cases-v015/primary-ui-verification.json)과 [설치 CLI 무결성](../reports/camera_platform/cases-v015/primary-integrity.json)은 기존 Builder·새 사례 개요·원본 행과 Artifact 보존을 확인합니다. [설치 QA 정리](../reports/camera_platform/cases-v015/installed-qa-cleanup.json)는 별도 API/Worker 종료와 QA 원본 상태 보존을 확인합니다.

[최종 소스 수정 시험 73개](../reports/camera_platform/cases-v015/availability-tests-final.xml), [Node 사례 상태 13개와 캐시 검증](../reports/camera_platform/cases-v015/availability-browser-tests.tap), [패키지 도구 시험 18개](../reports/camera_platform/cases-v015/package-verifier-tests.xml), [설치 Runtime 도구 시험 23개](../reports/camera_platform/cases-v015/installed-verifier-tests.xml)는 서로 다른 검증 범위입니다. [실제 wheel 설치 실행](../reports/camera_platform/cases-v015/package-installed/package-proof.json)은 소스·정적 파일 136개와 제품 payload 684개의 일치, 설치된 코드의 Response DB 계산·RAW/RGB 다운로드 해시·리뷰 이력을 확인합니다. [설치 API·Worker·재시작 21항목](../reports/camera_platform/cases-v015/installed-runtime/verification.json)과 [설치 UI 7항목](../reports/camera_platform/cases-v015/installed-ui/verification.json)은 실제 실행·저장·재열기·근거 누락 격리/복구를 확인합니다. UI에서 추가한 리뷰의 페이지 재열기를 프로세스 재시작 시험으로 확대하지 않습니다.

[변경 범위 승인·기존 전환 도구 110개](../reports/camera_platform/cases-v015/case-delta-transition-tests.xml)는 새 전환 Gate 32개와 기존 전환 시험 78개의 별도 실행입니다. 기능 시험·검증 도구 시험·설치 실행의 숫자를 한 완료율이나 전체 통과 수로 합산하지 않습니다.

[이전 후보 검증](../reports/camera_platform/cases-v015/evidence-summary.json)의 Python 132개, Node 12개와 캐시, HTTP/Worker 16항목, CUA 14항목 및 [백업 복원](../reports/camera_platform/cases-v015/backup-roundtrip.json)은 근거 누락 격리 수정 전의 역사적 증거입니다. 시험 수는 범위가 다르거나 겹치므로 합산하지 않습니다. [변경 범위 승인](../reports/camera_platform/cases-v015/install-candidate-source-v3.json)과 [전환 근거](../reports/camera_platform/cases-v015/case-delta-proof.json)에 따라 기존 물리 계산 증거는 **변하지 않은 payload와 의존성으로 승계**하며, 이번에 전체 광학·시간·장면 계산을 재실행한 것은 아닙니다. 원문 7개 모듈·4개 실행 방식의 전체 상용화 준비 수준은 **55–60%의 정성적 추정**이고 정량 요구사항 완료율이 아닙니다. 독립 실측은 0건이며 총 예측 불확실도는 `unknown`입니다.

패키지 검증은 같은 Mac의 기존 계산 의존성을 사용하는 TestClient이고, 별도 Worker 근거는 위 설치 Runtime 보고서에 있습니다. 기본 작업실의 공유 의존성은 아직 독립 환경으로 교체하지 않았습니다. 깨끗한 고객 OS 설치, 외부 Meep/Mitsuba와 빌드 의존성을 포함한 독립 배포, 지속 운영·권한·승인은 미완료입니다.

**이전 v0.14.0 인도 기록:** [처방 기반 OPD·위치별 PSF](camera-platform-prescription-optics.md)를 같은 처방의 광량·입사각 응답과 연결하고, 물리 상고가 고정된 Native RAW/RGB와 ISP 재처리에 사용합니다. [당시 기본 작업실 실행](../reports/camera_platform/prescription-primary-delivery.json)은 64×64 영상, RAW 불변·화이트밸런스 변경 RGB, Factory 부모를 포함한 근거 ZIP을 확인했습니다. 실측·총 불확실도·일반 광학 모델의 완료를 뜻하지 않습니다.

[전환](../reports/camera_platform/primary-prescription-upgrade.json)은 백업과 기존 **215개 기록·74개 작업·437개 감사 이벤트** 보존을 확인했습니다. [무결성](../reports/camera_platform/primary-prescription-integrity.json)과 [기존 시간 촬영 재현](../reports/camera_platform/primary-prescription-legacy-rerun.json)을 통과했습니다. [후보 감사](../reports/camera_platform/prescription-v014-candidate-audit.json)의 중복 제외 **1,450개 시험**, 설치 격리·재시작 20항목과 함께 설치 QA에서 처방 10·준비 20·작성 36·재사용 26개 UI 항목을 확인했고, [기본 작업실 조회 12항목](../reports/camera_platform/prescription-ui/primary-v014/verification.json)은 모든 DB 행을 유지했습니다. 외부 의존성은 아직 공유 환경이며 아래 독립 환경 작업은 별도입니다.

**독립 설치 추가분:** 기존 공유 계산 환경의 의존성 불일치를 발견하고 별도 가상 환경 설치 도구를 추가했습니다. [설치 근거](../reports/camera_platform/independent-environment/evidence-summary.json)는 정상 의존성 해결과 92개 입력의 잠금 재생을 확인합니다. [후속 근거](../reports/camera_platform/independent-environment/runtime-evidence-summary.json)는 잠금 재생 환경의 실제 통합 계산, r2 환경의 별도 API/Worker·처방 RAW/RGB·재시작과 CUA 저장 결과 조회를 확인합니다. 기본 작업실의 공유 의존성 환경은 아직 교체하지 않았으며, 외부 Solver·빌드 의존성·고객 OS·전체 화면 회귀와 독립 환경으로의 기본 실행기 전환은 남아 있습니다. [설치 범위와 한계](camera-platform-independent-environment.md)를 참고하세요. [이전 사례 연결 검토](../reports/camera_platform/case-workflow-gap-review.json)는 v0.14의 저장 계층 공백을 기록하며, 해당 사례 저장 기능은 위 v0.15에 추가됐습니다.

**이전 v0.13.0 인도 기록입니다.** [저장된 분광 복사휘도 재사용](camera-platform-scene-projection.md)을 기본 설치 작업실에 연결했습니다. 성공한 장면의 원본 `Lλ`를 보존하고, 같은 시점·초점거리·Native 격자·파장에서 새 투과율과 조리개로 센서면 광량을 계산한 뒤 대상 카메라의 RAW/RGB를 생성합니다. 조건이 달라지면 재렌더링을 요구하며, 원본 렌더링과 새 변환의 출처·환경을 별도 보존합니다.

[v0.12→v0.13 설치 전환](../reports/camera_platform/primary-projection-upgrade.json)은 백업을 만들고 기존 **212개 기록·71개 작업·425개 감사 이벤트**를 보존했습니다. [기본 작업실의 재사용 실행](../reports/camera_platform/primary-projection-verification.json) **9항목**, [기존 시간 RAW/RGB 재실행](../reports/camera_platform/primary-projection-legacy-rerun.json), [후속 무결성 검사](../reports/camera_platform/primary-projection-integrity.json)를 확인했습니다. 새 예제와 재실행은 **3개 기록·3개 작업·12개 감사 이벤트**를 추가했고 원래 기록은 유지했습니다. 기본 작업실의 [읽기 전용 화면 검사](camera-platform-installed-runtime.md#기본-작업실의-화면-검증)는 **7개 흐름·168항목**을 통과했습니다(POST 0회·브라우저 오류 0건).

`ready`와 계산 성공은 제한된 입력·실행 계약을 확인한 상태입니다. 실물 예측 정확도는 미확립이고 총 불확실도는 `unknown`이며, 같은 원본에서 재사용한 MC 표준오차는 공유 표본의 수치 진단입니다. 실제 장비 측정 데이터는 **0건**입니다. 독립 실측, 일반 광학/픽셀/전기 모델·HDR/LFM·고급 최적화·Agent·권한·고객 환경 운영을 포함한 **원문 일곱 모듈·네 실행 방식의 전체 Goal은 미완료**입니다. 설치 검증은 기존 계산 의존성을 공유하며 깨끗한 OS 설치를 보장하지 않습니다.

## v0.13 인도 추가분과 원문 대응

[저장 분광 복사휘도 재사용](camera-platform-scene-projection.md)은 개발 소스·별도 설치 QA·설치본 재시작·기본 작업실 실행에서 검증한 추가분이다. 아래 범위를 원문 전체 요구의 완료로 확대하지 않는다.

| 원문 요구 | v0.13 증거가 확인한 추가 범위 | 남은 범위 |
| --- | --- | --- |
| §5.3 입력 변경별 재계산 | 저장된 원본 `Lλ` → 새 투과율·조리개 → 대상 카메라 RAW/RGB; 원본 Renderer 재실행 없이 별도 작업·계보 보존 | 일반 단계별 실행 DAG, Readout 이후 부분 캐시, Pixel/FDTD 변경의 일반 의존성, AE 시간 흐름 캐시 |
| §7.1 분광 자산 재사용 | 파장별 복사휘도 원본 바이트 사용; 원본 투과율 0에서도 새 광량 계산; 같은 Native 격자에서 제한된 Lens/Sensor/ISP 변경 | 새 시점·Crop·초점거리·Pitch·파장의 재표본화, 일반 SpectralRayBundle과 유한 동공 결상 |
| §9 물리 입출력 계약 | 고정 Scene/조건/격자, 대상 카메라의 실제 Runtime 검사, 새 투과율 출처, 미입력·불일치 차단 | 일반 모델 구조·각도·편광·전기 Readout/HDR 계약 |
| §8 검증 구분 | 원본/새 광량/MCSE/RAW/RGB 바이트와 과거 렌더링/새 변환 환경 분리; 공유 표본 MCSE를 수치 진단으로 제한 | 독립 실측 0건, 모델·측정·근사 오차를 포함한 총 불확실도 및 물리 정확도 |
| §10 실행·근거 | 작업 큐·API/SDK/UI·원자적 자산 등록·재시도, 근거 ZIP, 새 기능 전환 Gate, 설치본 재시작과 기본 작업실 이력 보존 전환 | 독립 의존성·고객 운영·권한 |

[전체 개발 회귀](../reports/camera_platform/projection-development-tests.xml) **1,216개**와 [실행 전후 소스·환경 결합](../reports/camera_platform/projection-development-test-binding.json), [별도 전환 시험](../reports/camera_platform/projection-runtime-transition-tests.xml) **78개**와 [직접 입력 결합](../reports/camera_platform/projection-runtime-transition-tests-binding.json), [Node 상태 회귀](../reports/camera_platform/scene-projection-ui-state-regression.json) **6항목**, [개발 브라우저](../reports/camera_platform/projection-ui/development/verification.json) **26항목**을 확인했다. 전환 78개 중 기존 36개는 전체 회귀와 겹치며 42개가 추가분이다. [v0.13 Wheel 검사](../reports/camera_platform/release-archives/v0.13.0/package-verification.json)는 소스·정적 파일 132개 일치와 Checkout 밖 실제 실행을 확인한다. 별도로 [설치본 격리·재시작 17항목](../reports/camera_platform/projection-installed-runtime-verification.json), 설치 QA의 [재사용 26항목](../reports/camera_platform/projection-ui/installed-v013/verification.json)·[준비 20항목](../reports/camera_platform/projection-ui/preparation-v013/verification.json)·[작성 36항목](../reports/camera_platform/projection-ui/authoring-v013/verification.json), 위 기본 전환·실행 근거가 있다. 공유 의존성을 사용한 로컬 검사로 제한한다.

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

**v0.10.0 인도 전 개발 검증 기록:**
[폐루프 공통 RAW 관측·비교](camera-platform-control-observation.md)를 공통 Test·Worker·SDK·취득
업로드·검증·근거 ZIP에 연결했다. 실제 적용 설정이 서로 다른 응답도 비교하고 누락·시계 대응과
불확실도를 보존한다. 후속으로 [취득 원본과 분석 Revision](camera-platform-capture-analysis.md)을
분리하고, 같은 원본의 여러 지표·공통 Test 작성·분석 이력 선택과 재계산 화면을 연결했다.
회귀 시험 1,082개, 재분석 HTTP/Worker 5항목·브라우저 21항목을 확인했다. 전용 응답 RAW/로그
업로드 화면, 총 불확실도·실측, 새 설치 패키지 인도는 남아 있다.
아래 v0.9.0 설명은 당시 설치본의 역사적 범위다. 전용 업로드와 설치 패키지는 위 v0.10.0 인도에서 완료했다.

**v0.9.0 도입 기록.** [AE/AWB 제어](camera-platform-closed-loop.md)는
불변 정책·촬영 프로그램, 전체 노출/Gain 범위 검사, 공통 시험·API·SDK·별도 Worker,
32프레임 촬영과 RAW 재처리를 작업실에서 연결합니다. 요청·양자화·실제 적용과 목표 잔차를
보존하며, 현재 폐루프 관측을 기존 비교기로 물리 검증 PASS 처리하지 않습니다.
[패키지 검증](../reports/camera_platform/package-verification.json),
[설치본 격리·재시작 검증](../reports/camera_platform/controls-installed-runtime-verification.json),
[기존 기록 보존 전환](../reports/camera_platform/primary-controls-upgrade.json)을 확인했습니다.
전체 상용화 Goal은 진행 중이며 독립 실측, 일반 3D/물리 모델, HDR/LFM, 전체 불확실도,
고급 최적화·Agent·권한·고객 환경 배포는 남아 있습니다.

## v0.9.0 도입 당시 범위와 남은 요구사항

| 원문 요구 | 현재 증거가 확인한 범위 | 남은 범위 |
| --- | --- | --- |
| §7.3 폐루프 제어 | RAW 기반 AE/AWB, 독립 지연·제어 격자·실제 노출/WB 적용, 최대 64프레임 계약과 운영 32프레임 예제 | HDR/LFM, 일반 ISP Graph, 실제 장비의 제어·전송 모델 |
| §8.1 공통 시험 | 장면·정책·프로그램·일정·모델 버전 고정, 생성된 실제 설정 보존 | 실측 장비 취득, 제어 궤적 관측 연산자와 정합된 비교 |
| §8.2 검증 구분 | 구현 시험·설치본 재현성 확인; 폐루프의 기존 RAW 비교는 INCONCLUSIVE | 독립 실측 정확도와 전체 불확실도 검증 |
| §9.3 실행 전 정합 | 전체 AE 노출/Gain 구간의 모델 유효 영역 포함, 고정 광학·환경·RAW·시간 조건 검사 | 일반 전기 Readout/HDR 모드와 복잡한 광학·픽셀 모델의 호환성 |
| §10 실행·저장 | 불변 원본·해시, API/SDK/별도 Worker, 설치본 격리·재시작, 기존 기록 보존 | 독립 의존성, 고객 환경 설치·이전·부하, 저장소 확장과 보존 정책 |
| §11 Agent | 사람과 동일한 API/SDK 경로 및 작은 작업실 요약 | 요구사항→실험 계획→결정의 Agent 흐름, 역할별 승인·권한 |

원래 일곱 모듈과 네 실행 방식, 일반 3D·물리 모델·HDR/LFM·전체 불확실도·고급 최적화·
사례/결정·상용 운영을 완료했다고 판정하지 않는다. 이전 표의 미연결 항목은 해당 버전의
기록이며, AE/AWB의 현재 연결 상태는 위 표와 v0.9.0 인도 근거를 따른다.

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

아래 기반 감사의 기준일: 2026-09-13. 최신 인도 추가분은 위 v0.14.0 근거를 따릅니다. **주변부 Color shading은 전체 플랫폼을 검증하기 위한 첫 개발 사례이며, 사용자 목표의 최종 범위가 아니다.** 원문의 일곱 모듈, 네 실행 방식, 장면·시간·최적화·Agent·권한 요구를 계속 유지한다. v0.4.0에는 선언 동공의 Prysm PSF Factory, Native 격자의 분광 이상 영상→PSF→실제 RAW/RGB, 보존 RAW의 명시적 ISP 재처리가 추가되었다. 3D Scene·시간·일반 동공/센서 모델이나 상용제품 전체의 완료를 뜻하지 않는다.


## v0.5 도입 당시 검증 기록

기본 작업실은 추가로 [검증된 설치 Runtime](camera-platform-installed-runtime.md)에
고정되었습니다. [독립 실행 검사](../reports/camera_platform/installed-runtime-verification.json)는
개발 Checkout 변경과 재시작 뒤 실제 RAW가 유지되는 것을 확인합니다.
[기본 작업실 전환](../reports/camera_platform/primary-installed-runtime-verification.json)에서는
동일 v0.5의 기존 기록을 보존하고 RAW/RGB를 재현했습니다. 코드·화면·데이터의 분리이며,
공유 의존성·깨끗한 OS 설치·다른 버전 DB Migration과 복귀는 미완료입니다.

이 문서의 v0.4 상세표는 앞선 공간·Factory 인도의 범위를 보존한다. 시간·기록 설정의
현재 상태는 아래 추가분으로 갱신한다. 일곱 모듈·네 실행 방식과 미완료 상용화 Gate는
축소하지 않는다. v0.5 역시 전체 제품 완료나 실물 정확도 승인이 아니다.

| 요구 | v0.5에서 추가된 실제 경로 | 남은 범위 |
| --- | --- | --- |
| 시간 장면·기록 조건 Replay | 시각별 분광 영상과 Clock, 선형/ZOH 적분, Native Rolling/Global 노출, 프레임별 적용 노출·Gain·ISP→실제 RAW/RGB | 3D 기하학적 움직임·가림, 연속 장면 근사 오차, 실제 HW 동기 정확도 |
| 명령·적용·지연 분리 | 별도 명령 ID와 적용 기록, 제공된 시각만으로 지연 계산, 미제공 지연은 unknown | 폐루프 AE/AWB, HDR Gain/노출·Merge, 전자·전송 지연 모델 |
| 원본과 비교 | 시퀀스 JSON 원문·작성 Recipe·모든 NPY, 프레임 미리보기, Clock/순서/적용 조건 비교, 프레임별 ISP 유효 범위 | 장비 취득 Adapter·독립 실측, 불확실도 전파 |
| RAW 재처리·보존 | 원본 프레임별 촬영 조건과 RAW 보존, 새 ISP를 별도 표시, 근거 ZIP·백업/복구 확인 | 임의 ISP Graph·HW/Fixed-point Backend, 제어 상태 재연산 |

v0.5 당시 소스의 [회귀 시험](../reports/camera_platform/temporal-tests.xml)은 플랫폼과 관련
센서/시간/HW ISP/데이터셋 870개가 통과했다. [시간 HTTP 검증](../reports/camera_platform/temporal-http-verification.json)
7항목은 실제 Worker 계산·재실행 해시·변경 ISP·원본 ZIP을 확인한다.
[브라우저 검증](../reports/camera_platform/temporal-ui/headless-qa.json) 36항목은 독립 Headless Chromium에서
실제 장면/설정 등록→시험→CameraRun→프레임 탐색→ISP 재처리 경로를 검사한다.
기존 Core HTTP 10항목, RayOptics/Meep 5항목, 공간/재처리 6항목도 v0.5에서 재실행했다. 기본 작업실의 [읽기 전용 시간 UI](../reports/camera_platform/temporal-ui/primary/primary-readonly-qa.json) 31항목은 POST 0회로 확인했다. [설치된 Wheel](../reports/camera_platform/package-verification.json)은 Checkout 밖에서 세 Solver·공간·시간 촬영·RAW 재처리를 확인하며 기존 의존성을 재사용한 범위다.
보고서마다 기록한 소스·환경과 검사 범위에 한정한 구현 증거다.

## 기준과 증거의 범위

- 원문: [첨부 요구사항 전체 882행](/Users/seongcheoljeong/.codex/attachments/05c0d550-242f-4f9c-aa3a-5cf9b18ea539/pasted-text-1.txt). SHA-256 `b353bd80aea13a82e8afe2c8d3610dd74f190951333017780421ea39dd8a3611`. 아래 `§`는 원문 절 번호다.
- 구현 기준: `src/camera_platform/`의 v0.4.0 계약·서비스·Runtime·Factory와 기존 `src/pyisetcam/`의 재사용 가능한 기능을 확인했다. 기존 엔진에 있는 기능을 플랫폼에 연결되었다고 계산하지 않는다.
- v0.4 회귀·HTTP 증거: [전체 회귀 JUnit](../reports/camera_platform/tests.xml)의 674개 시험이 실패·오류·Skip 없이 통과했다. 현재 버전으로 실행한 [HTTP→Worker 검증](../reports/camera_platform/http-worker-verification.json) 10항목, [RayOptics/Meep Factory HTTP](../reports/camera_platform/factory-http-verification.json) 5항목, [Prysm→Native Scene→Camera→ISP Replay HTTP](../reports/camera_platform/imaging-http-verification.json) 6항목도 통과했다. 보고서의 실행 환경·소스 해시가 해당 증거의 범위다.
- v0.4 화면·설치 증거: [공간·PSF·Replay UI](../reports/camera_platform/spatial-ui/headless-qa.json) 60항목 PASS는 실제 Prysm·공간 Camera·변경 ISP와 원본 RAW 동일성·근거 ZIP을 확인한다. 이 브라우저 경로의 Replay 원본은 완료 Camera Run이며 Capture 원본의 브라우저 흐름은 검증했다고 주장하지 않는다. 초기 화면의 기능 증거와 별도로, 확대 미리보기·쉬운 실행 방식 명칭을 반영한 [최종 기본 작업실 검사](../reports/camera_platform/spatial-ui/primary-readonly-qa.json)는 32항목 PASS·POST 0회이며 실제 원본 크기·표시 배율과 원본 해시 유지, Desktop/Mobile 렌더링을 확인한다. [설치된 Wheel 검증](../reports/camera_platform/package-verification.json)은 Checkout 밖 `python -I`에서 세 Solver·Native 공간 계산·Replay·정적 파일과 Artifact 해시를 확인했다. 기존 의존성을 재사용했으므로 깨끗한 OS/Dependency-resolution 시험은 아니다.
- 이전 화면 증거: [관측/보정 UI](../reports/camera_platform/ui/headless-qa.json) 22항목과 [RayOptics/Meep Factory UI](../reports/camera_platform/factory-ui/headless-qa.json) 112항목은 당시 범위의 역사적 기록이다. 현재 HTTP·패키지 보고서와 구분하며 새 Prysm·공간·Replay의 증거는 위 v0.4 보고서를 사용한다.
- v0.4 직접 구현 증거: [PSF의 독립 Airy·복소 OTF·조합 범위 시험](../tests/platform/test_psf_factory.py), [공간 Runtime 시험](../tests/platform/test_spatial_runtime.py), [ISP 재처리 시험](../tests/platform/test_isp_replay.py), [실제 Prysm→공간 Camera→ISP Replay→근거 ZIP 통합 시험](../tests/platform/test_spatial_workflow.py). 원본 분광 영상·PSF와 단계별 배열, 바뀐 RGB와 동일한 RAW, 취득 조건과 출처 계보를 확인한다. 기존 [분광 적분 시험](../tests/platform/test_spectral_lens.py)과 [Factory 흐름](../tests/platform/test_factory_workflow.py)도 회귀 근거다.
- [API 계약 시험](../tests/platform/test_imaging_api.py)과 위 실제 HTTP·UI·패키지 증거는 구현 경로를 확인한다. 예상 시험 수나 진행 중 상태를 PASS로 기록하지 않으며, [인도 기록](../reports/camera_platform/delivery.json)은 반드시 명시된 버전·소스 해시와 함께 읽는다. 이 증거는 실물 정확도·지원 OS 전체·지속 부하·상용 승인까지 포함하지 않는다.
- `부분 구현`은 지정한 경로가 실제 동작한다는 뜻이다. `플랫폼 미연결`은 기존 엔진 기능이 있어도 공개 계약·Registry·Worker·비교 경로에서 사용할 수 없다는 뜻이다. 실측 미확보는 이 두 상태와 별개의 증거 상태다.

## 핵심 연결 결함의 해결 범위

| ID / 영향 | 확인한 코드와 현재 문제 | 필요한 수정·완료 증거 |
| --- | --- | --- |
| I1 / 시험 의미와 성공 상태 | 감사에서 발견한 “MTF 시험 ID로 RAW 비율 계산 성공” 경로는 [service.py](../src/camera_platform/service.py) `_prepare_run()`의 metric/unit/reference plane/effects 검사로 차단했다. 시험 사전 등록과 특정 Runtime의 계산 지원을 구분한다. | **현재 지원 범위에서 수정·회귀시험 완료.** `test_runtime_refuses_unimplemented_measurement_contract_before_queue`는 잘못된 Metric·단위·기준면·효과 및 Quick의 비지원 시험을 Queue 전에 거부한다. MTF/QE/SFR 계산 Runtime 자체가 구현된 것은 아니다. |
| I2 / 새 Solver 결과의 실제 소비 | RayOptics→분광 Lens와 Prysm→픽셀 적분 PSF 후보를 실제 Camera 계산에서 소비한다. [factory.py](../src/camera_platform/factory.py)는 PSF 조합 시 부모 유효 범위·파장 좌표·명시된 F-number와 훈련 계보를 보존한다. [spatial.py](../src/camera_platform/spatial.py)의 분광 영상·PSF·전자·실제 RAW/RGB와 원본 Export를 [통합 시험](../tests/platform/test_spatial_workflow.py)으로 확인한다. | **제한된 분광 Lens·선언 PSF·평면 영상 경로에서 해결.** 실제 Lens Wavefront 추출, 시야별 PSF/왜곡·재질 손실·Pixel 전기 응답·3D Scene은 남아 있다. PSF와 RI/각도 응답의 분리 적용은 근사다. Meep 흡수는 전자 QE 후보를 만들지 않는다. |
| I3 / 실측을 넣어도 닫히지 않는 검증 경로 | v0.6에서 `parameter_uncertainty`가 실제 Runtime과 동일 ROI 출력까지 연결됐다. 출처·단위·고정 버전과 조건부 입력 경계를 보존한다. | **입력 경계 부분 전파 구현·검증.** 실물 모델 불일치·연속 적분/보간·관측 통계의 전체 경계는 unknown이다. PSF·시간·ISP 확장과 독립 실측 검증이 필요하다. |
| I4 / 보존된 RAW와 재처리 Runtime의 단절 | [isp.py](../src/camera_platform/isp.py)의 BLC→Bilinear→고정 WB→CCM→Tone와 [imaging.py](../src/camera_platform/imaging.py)의 별도 Replay 작업으로 연결했다. 등록 Capture 또는 완료 Camera Run의 RAW·Native/대표 격자·취득 조건을 유지하고 새 ISP 프로필의 전체 프레임·중간 배열을 보존한다. | **지원 RGGB/고정 Float64 그래프에서 해결.** WB/CCM을 바꾸면 RGB가 달라지고 RAW·취득 조건은 동일함을 시험한다. 임의 Graph·LSC·HW/고정소수점 ISP·동적 제어 Replay는 남아 있다. ISP 재처리는 다른 센서·노출의 재촬영이 아니다. |

I1은 지원 범위의 의미 결함을 수정했고, I2·I4는 제한된 실제 실행 경로로 연결되었다. I3와 I2·I4의 일반 공간·전기·시간·ISP 확장은 미완료다. 수치 수렴이나 훈련 잔차를 예측 불확실도 0 또는 독립 물리 검증으로 바꾸어 공백을 없애면 안 된다. 외부 실측 확보와 함께 남은 소프트웨어를 계속 구현·시험할 수 있다.

## 일곱 모듈: 요구 → 현재 근거 → 남은 범위

| 모듈 / 원문 | 현재 구현과 확인 가능한 근거 | 아직 필요한 기능 |
| --- | --- | --- |
| ① Asset Registry / §2–3, §10 | 불변 자산·모델·시험, RAW/Dark·Measurement 원문에 더해 분광 이상 영상 NPY·Native crop/파장/조건, PSF Factory와 ISP 프로필·Replay 원본 연결을 보존한다. [registry.py](../src/camera_platform/registry.py), [imaging.py](../src/camera_platform/imaging.py), [backup.py](../src/camera_platform/backup.py), Registry/Backup/공간 통합 시험이 근거다. **부분 구현.** | LensDesign/PixelStructure/PhysicalInstance/PropertyDataset의 일반 도메인, 축을 가진 대용량 배열, 3D 재질·광원·Scene 연결, 승인/폐기와 접근권한. 제한된 평면 영상 계약과 Asset kind만으로 전체 물리 타입 계약이 완성된 것은 아니다. |
| ② Model Factory / §1–2, §4, §9 | 실제 RayOptics 구면/평면 Lens, Meep 수직 입사 평면 손실층, [psf_factory.py](../src/camera_platform/psf_factory.py)의 Prysm 0.21.1 선언 동공→픽셀 적분 PSF·복소 Kernel OTF. 입력·환경·수치 검사·원본·후보 계보를 연결한다. [test_psf_factory.py](../tests/platform/test_psf_factory.py)는 독립 Airy 적분과 수차·위상·조합 유효 범위를 시험한다. RAW/Dark→제한 R/B 보정도 유지한다. **제한된 광학 특성 계산·보정 구현.** | 실제 Lens Wavefront 추출, 비구면·비대칭·일반 재질/코팅·시야별 PSF, 2D/3D Pixel·편광·Crosstalk, 명시적 광흡수→수집전자 변환, 일반 축약모델·독립 검증·승인. 선언 동공의 결과를 RayOptics 처방의 실제 PSF로, Meep 흡수를 전자 QE로 바꾸지 않는다. |
| ③ Camera Composer / §2, §4, §9.3 | [composer.py](../src/camera_platform/composer.py): 정확한 모델 버전, Image circle/센서 대각선, 파장·각도 범위, 기준면, 효과 중복, RAW 형식·운용 조건 검사. 분광 Lens 모델의 정확한 파장 좌표와 Factory 조건·원본 연결을 검사하고 새 Assembly에서 재사용한다. [test_composer.py](../tests/platform/test_composer.py), Factory 통합 테스트. **부분 구현.** | 초점/조리개/물체 거리·Tilt/Decenter/공차 변경에 따른 모델 선택·재계산, 방위각·편광, 일반 광원/재질 파장 샘플링, 지원 Sensor mode와 제어 범위, 임의 ISP/HDR Graph. 현 서비스가 허용하는 3모델/고정 RAW 구성 밖의 연결 계약. |
| ④ Simulation Runtime / §2, §5, §7 | Quick/정적 Shading에 [spatial.py](../src/camera_platform/spatial.py)의 Native 분광 이상 영상→파장별 PSF→RI/각도 QE→실제 RAW→명시적 ISP를 추가했다. [test_spatial_runtime.py](../tests/platform/test_spatial_runtime.py)는 Delta·Convolution 방향·절대 광량·경계 에너지·CFA·실제 출력 변화를 확인한다. 보존 RAW의 ISP Replay도 별도 작업으로 실행한다. **Camera 계산·재처리와 Detailed Characterization의 제한 범위 구현.** | 일반 3D Scene/재질/조명·거리·시야별 PSF/왜곡·고급 ISP·시간 제어·Perception Metric. 평면 분광 영상 입력과 단일 PSF의 공간 계산이 전체 공간·시간 Camera Runtime을 완성한 것은 아니다. |
| ⑤ Validation Center / §8 | [validation.py](../src/camera_platform/validation.py): 의미/축/조건/범위/불확실도/독립성, 세 검증 종류 및 보류·범위 밖·비교 불가 상태. [observations.py](../src/camera_platform/observations.py): 실제/예측 공통 ROI 연산자·원본 재분석, 프레임 재사용 방지. Factory의 해석적 기준·부호 있는 광량·수치 수렴 검사와 분광 적분 시험은 구현 검증 근거로 보존한다. **RAW 채널 비율 중심 부분 구현.** | 장비 제어·실제 적용 설정 로그, Chart/SFR/PSF/EMVA 등 관측 Adapter, 조건별 검증 Coverage와 회귀 영향, 불확실도 전파·독립 Holdout 평가와 승인. 해석해 일치·수치 수렴·합성 Holdout은 실물 성능의 검증이나 알려진 예측 오차 경계가 아니다. |
| ⑥ Experiment & Optimization / §2, §5.3, §10.2–11 | `ExperimentCreate`, `service.create_experiment()`: Assembly×노출, 최대 24 Run, 사전 검사·고정 시드·조건 Snapshot. UI는 A/B와 4/8 ms 사례. **Sweep 부분 구현.** | 일반 설계/센서/ISP/제어 파라미터 탐색, 민감도·공차·환경 변화, 다목적 Pareto/Trade-off, 제약·비용 기반 최적화, 적용 범위 공백을 채우는 적응형 추가 해석/실측. |
| ⑦ Agent & Release Control / §10–12 | 공통 API·SDK, 감사 Chain, 미승인 Release와 차단 사유, [evidence.py](../src/camera_platform/evidence.py)의 계보·원본 ZIP. 보정 훈련 자료·Factory·분광 Scene·원본 RAW와 변경 ISP까지 묶어 오프라인 근거로 보존한다. **실행·근거 준비 부분 구현.** | Agent의 요구→Metric/제약→계획/비용→실행→추가 실험 정책, 공통 보조 패널, 사용자/프로젝트/자산 역할, 작성자/승인자 분리, Golden baseline 권한, 승인/철회·서명·배포·변경 영향 추적. 후보 등록과 수치 검사 통과는 승인이나 배포 권한이 아니다. |

## 네 실행 방식과 실측 RAW Replay

| 실행 방식 / §5 | 기반 구현과 v0.8 Surrogate 추가 | 빠진 계산·계약과 완료 확인 |
| --- | --- | --- |
| Quick Estimate | 센서면 분광 광량×유효 QE×면적×노출의 적분, 전자 수·비포화 이론 SNR·포화 여유. `_common()`, `_integrate()`, `QuickEstimateRuntime`. | 해상도/왜곡 추정, F-number→센서면 광량 변환, 일반 민감도 연결. 현재 광량은 Lens 투과/조리개 영향이 이미 반영된 입력이므로 F-number를 입력했다고 자동 계산되지 않는다. |
| Response-DB Simulation | 정적 Shading은 대표 상고 Raster로 유지한다. **추가 공간 경로 `spatial_scene_v1`**은 정확한 파장·Pitch·Native crop의 분광 이상 영상에 파장별 픽셀 적분 PSF를 Convolution하고, RI·각도별 QE·전자·Digital RAW·튜닝 ISP를 계산한다. 경계 밖으로 나간 에너지를 별도 기록한다. | 입력은 센서 이상 영상면의 광량이며 3D 렌더러 출력이 아니다. 소스 Cell 중심점 적분, 공간 불변 PSF와 출력 위치별 RI/각도 응답의 분리 근사. 깊이·가림·거리/시야별 PSF·왜곡·일반 재질·방위각·편광·Crosstalk·Timing/HDR/제어·Task Metric은 남아 있다. |
| Surrogate Prediction / v0.8 | 설계 실험의 불변 원본 Run에서 비선형 RBF 계수 학습, 별도 좌표 검증, 고정 오차 기준과 범위 이탈 거부, UI/SDK 일괄 수치 예측과 이력 저장. 학습 범위의 모든 모서리와 출처·단위·계산 환경 일치를 확인한다. | 현재 한 카메라·시험의 수치 지표 근사다. 일반 Lens/Pixel/ISP/영상 응답, 연속 영역의 오차·불확실도, 적응형 추가 계산, 독립 실물 검증과 승인 연결은 남아 있다. |
| Detailed Characterization | **부분 구현: 제한된 광학 특성 계산.** RayOptics의 분광 RI/각도 분포, Meep의 signed R/T/A·독립 수치 검사, Prysm의 선언 원형 동공→파장별 Pixel PSF·복소 Kernel OTF·독립 동공/수신 격자 비교. 실제 새 조합과 공간 RAW/RGB에서 재사용하고 원본·환경·실패·취소를 보존한다. | 실제 Lens Wavefront와 일반 Lens/Pixel 구조, 시야별·벡터 회절/편광·공간 Crosstalk·수집전자·Readout/Noise/Timing 모델. Kernel OTF에는 Pixel aperture·유한 범위 정규화가 포함된다. Meep는 광학 결과만 보존한다. 전체 공간·시간 상세 Camera 모델이 아니다. |
| 실제 RAW 재처리 / §2, §7.3 | 등록 Capture 또는 완료 Camera Run의 동일 RAW→명시적 BLC/Bilinear/고정 WB/CCM/Tone→전체 프레임·중간 RGB. 취득 조건·Source kind·원본 해시와 새 프로필을 분리 보존한다. | 벤더 Decoder, 임의 Graph·LSC/고급 ISP·Fixed-point/HW Backend, 동적 상태/제어 로그 Replay, SFR/색 정확도 등 공통 Metric Adapter. 센서·노출을 바꾼 재촬영의 대체가 아니다. |

Quick Estimate, 정적 Shading, 공간 영상, RAW Replay와 Factory 작업의 공개 진입점은 구분된다. v0.3 Factory UI의 112항목은 이전 범위의 증거이며, v0.4의 공간·Prysm·Replay는 새 UI 60항목과 실제 HTTP 6항목으로 별도 확인했다. Surrogate를 포함한 네 실행 방식을 자유롭게 조합하는 일반 UI·계획 계층은 남아 있다.

원문 §14의 **2단계(조합 평가와 모델 생성)는 부분 달성**이다. RayOptics와 선언 동공 Prysm 출력이 DB에 등록되어 다른 조합·공간 계산에서 소비되는 연결을 확인했다. 실제 Lens Wavefront·일반 Lens/Sensor 모델까지 검증한 것은 아니다. 1단계 비교·재현 기반, 3단계의 제한 보정·고정 ISP Replay도 구현되어 있으나 독립 실측·동특성·다른 조건의 성능 악화 추적은 남아 있다. 4단계의 Agent·다목적 최적화·적응형 추가 계산/실측은 전체 목표에 포함한다.

## 누락되기 쉬운 교차 모듈 범위

| 원문 요구 | 현재 증거·제약 | 남은 소프트웨어 인도 단위 |
| --- | --- | --- |
| 특성 사전과 공통 타입 / §3.1, §9.2 | `PROPERTY_DICTIONARY`는 PSF/MTF/RI/n·k/QE/Read-noise/RAW-ratio/채널 평균 DN/SFR를 구분한다. Native 분광 영상·픽셀 적분 PSF·고정 ISP Profile 계약도 추가되었다. | Lens 설계/비정상광, Pixel 구조/전기/시간/HDR, 임의 ISP Graph, 모듈 공차, 3D Scene·재질, 시험·계측의 일반 Schema. 광흡수와 전자 QE, Pixel 포함 OTF와 순수 Optical MTF/RAW·ISP SFR, RI와 굴절률의 구분을 유지한다. |
| Geometry·Material·Light·Scene / §7.1 | [imaging.py](../src/camera_platform/imaging.py)는 분광 이상 영상의 NPY 원문·`W/m^2/nm`·정확한 파장·Native crop/Pitch·취득 조건을 등록하고 공간 Run에 연결한다. `measured/simulated/vendor/estimated` 출처를 보존하며 진위 인증과 구분한다. 기존 [scene.py](../src/pyisetcam/scene.py)의 일반 장면 기능은 아직 플랫폼에 모두 연결되지 않았다. | 3D 형상·좌표·규모·재질, 분광 반사/투과·BRDF/BSDF·광원의 공간/시간 분포, 깊이·가림·움직임·환경. RGB EXR / 분광 Band / 특정 QE 적분 출력의 구분과 근사 변환, Mitsuba/OpenMATERIAL 또는 동등한 Adapter. Native 평면 입력을 3D 렌더링 완료로 세지 않는다. |
| Lens–Sensor 정밀 경계 / §4.1–4.3 | 분광 Lens `angular_weights[wavelength,field,theta]`·RI와 Sensor `[channel,wavelength,theta]`를 결합한다. 추가 PSF는 정확한 파장/Pitch·픽셀 적분 계약으로 소비하며 수신 Pixel aperture를 다시 적분하지 않는다. 복소 OTF 위상과 유한 Kernel의 원래 에너지 비율도 보존한다. | 실제 Lens Wavefront·시야별 PSF, PSF/입사각 응답의 결합 분포, 방위각·편광·Pixel type·Crosstalk. 분리 적용·유한 꼬리 정규화 오차는 unknown이다. CRA를 분포로 대체하거나 샘플링된 Image circle을 영상 품질·MTF 보증으로 해석하지 않는다. |
| FDTD→전기 모델 / §4.2 | Meep Adapter는 파장 nm·층 두께 nm·명시적 n,k를 받아 수직 입사 평면 손실층의 광흡수를 계산한다. 기준/구조의 부호 있는 Flux와 정규화 근거, 수치 설정 비교를 보존하고 `model_candidate: null`을 반환한다. 플랫폼 Sensor QE는 여전히 수집전자/입사광자 기준이다. | 2D/3D PixelStructure·수집영역/재질·전기 경계의 계약, 광학 효율→전하 수집 모델, Readout/Noise/Timing과 분리. 기존 [fdtd_sensor.py](../src/pyisetcam/fdtd_sensor.py)나 외부 LUT의 존재만으로 이 연결을 완료로 세지 않는다. 음수 흡수에 절댓값을 취하거나 전기 파라미터를 추정해 QE로 등록하지 않는다. |
| ISP Graph/상태/구현 Backend / §7.2 | [isp.py](../src/camera_platform/isp.py)의 버전별 Float64 BLC→Bilinear→고정 WB→CCM→Tone 프로필. 공간 계산과 Replay가 비-identity WB/CCM을 실제 적용하고 전체 프레임·중간 배열·음수/클리핑을 보존한다. 기본 프로필은 기존 pyisetcam 출력과 일치한다. | 임의 Stage Graph·LSC/Denoise/Sharpen·통계/제어 상태, Fixed-point/실제 HW Backend, Sensor/ISP 내 HDR Merge 위치. 기존 [hwisp.py](../src/pyisetcam/hwisp.py)의 Stage/Sequence 기능 연결과 특정 상용 HW 등가성은 별도 작업이다. |
| 고정·기록 설정 Replay·폐루프 / §7.3, §14 | 보존된 RAW 전체 프레임을 고정 ISP로 재처리하고 원본 취득 조건을 유지한다. v0.5에서는 별도 플랫폼 시간 Runtime과 Scene/Control Sequence가 연결되었다. 기존 HW ISP의 폐루프 제어 Sequence 연결은 남아 있다. | 프레임별 기록 설정·제공된 지연 보존은 v0.5에서 구현했다. 후속은 AE/AWB 통계→다음 노출/처리, HDR Gain/노출, LFM 파형/위상·움직임·Readout. 정적 RAW 재처리를 이 동적 제어 Replay의 완료로 세지 않는다. |
| 의존 실행 Graph·부분 캐시 / §5.3 | 고정 Run 전체 입력/환경/시드 기반 캐시와 재생 해시를 확인했다. `registry.submit()`·`_prepare_run()`의 캐시 단위는 전체 Run이다. | CCM 변경 시 ISP 이후, Noise 변경 시 Readout 이후, Pixel 변경 시 FDTD 이후처럼 의존 DAG별 캐시 무효화·재사용. AE 변경은 시간 흐름 전체 의존성을 반영해야 한다. |
| 불확실도·조건별 신뢰도 / §8.2, §8.5 | Validator의 알려진 절대 경계 판정과 `unknown` 보류, 세 검증 종류 구분은 있다. 실제 Runtime 경계는 unknown. | 모델·계측·수치/보간·ROI 표본화의 정량 모델, 상관/공유 Dark의 처리, 출력 전파, 조건별 Coverage·미지원 효과 지도와 회귀 추적. 물리 검증 데이터 확보와 병행해 구현 가능한 범위다. |
| 적응형 DB와 Trade-off / §10.2–11 | 유효 범위 검사, 제한 Sweep과 보정의 Rank/조건수 진단. | 대표 조건 샘플링→검증된 보간→오차/불확실도가 큰 영역 선택→추가 해석/측정→새 모델. 비용·예산·제약/다목적 Pareto, 민감도/공차 분석, 중간 산출물 보존 정책. |
| 네 Plugin 역할 / §9.1 | `RuntimePlugin`의 capability/validate/resources/execute와 고정된 RayOptics/Meep/Prysm Characterization, 공간 Runtime·ISP 재처리 경로가 있다. 서버의 검증된 Meep Interpreter를 사용하며 Job에서 임의 Script를 받지 않는다. Measurement/Analysis는 직접 서비스 함수다. | 역할별 공통 등록/발견과 격리 정책, 일반 Characterization·장비 측정·분석 Adapter. 세 고정 Solver의 실행을 임의 Plugin 생태계 완성으로 세지 않는다. 함수명뿐 아니라 축·단위·수치 옵션·원본 계보를 검증해야 한다. |
| Agent·권한·승인·영향 / §11–12 | 공통 API·SDK, 로컬 Token, 불변 기록, `blocked/review_required` Release와 `unapproved` 상태. | Agent 계획·비용·도구 정책과 보조 UI, 프로젝트/자산/원본 Export 정책, 측정 원본·판정 기준·Golden baseline·최종 승인 권한 분리, 역방향 의존 영향 조회와 승인/철회/서명/배포. Token은 사용자·역할 인증이 아니다. |
| 저장·운영 교체 / §10 | SQLite·로컬 Blob·단일 Worker, 취소/Timeout/재시작·일관된 복구. [test_worker_process_group.py](../tests/platform/test_worker_process_group.py)는 취소·Timeout이 SIGTERM을 무시하는 손자 프로세스까지 종료하는지 확인한다. Factory 계보·Artifact의 Backup 복구도 통합 시험한다. | Metadata/Job/Blob 인터페이스 분리, Schema Migration, 작업 Lease·자원/동시 실행 관리, 축/Chunk 저장, 보존/정리·용량 정책. PostgreSQL/Xarray/Zarr/Prefect는 후보이며 정확히 그 제품을 채택하는 것보다 동등한 기능과 물리 계약이 필요하다. |

## 상용화 Gate와 전체 목표의 관계

[상용화 문서](camera-platform-commercialization.md)의 G1–G12는 유용한 출시 Gate다. 하지만 정적 Shading 범위에 한정된 Gate 통과만으로 원문 전체의 기능 완료를 의미하지 않는다. 문서의 “첫 제품 범위를 한정”하는 문장은 **개발 Slice/Pilot 제안**으로 읽어야 하며, 명시적인 사용자 범위 변경 없이 최종 요구를 삭제하는 근거가 될 수 없다.

| Gate 묶음 | 보존할 현재 증거 | 아직 남은 합격 근거 |
| --- | --- | --- |
| G1 설치 / G12 배포 | Wheel의 소스·정적 파일 일치, Checkout 밖 설치 경로와 실제 실행 Artifact 검사. | 기존 의존성을 재사용한 `--no-deps` 검증이므로 깨끗한 OS/Dependency-resolution 시험·지원 OS/잠금 환경·업그레이드/되돌리기·담당 승인 완료가 아니다. |
| G2–G4 계약·구현·실제 출력 | 단위/면적/노출/잡음/양자화, Composer·시험 거부, 실제 RAW/RGB·시드 재현성. RayOptics 분광 Lens, Meep 광흡수와 Prysm PSF·Native 평면 영상·변경 ISP·동일 RAW의 직접 시험 및 실제 HTTP·UI·설치된 Wheel 실행을 확인했다. | 전체 출시 계약, 3D/시야별·시간 Runtime 등 남은 기능, 100회 반복 목표를 별도로 확인한다. 제한 공간 계산의 증거를 전체 상세 Camera Runtime 합격으로 확대하지 않는다. |
| G5–G6 실물·불확실도 | 독립성·불확실도 보류·훈련 계보 차단과 RAW 관측 구현 증거. | 모델 불확실도 전파 소프트웨어와 실제 계측/Holdout 데이터가 모두 필요하다. 실측 데이터를 기다린다는 이유로 불확실도 전파나 Scene/동특성/Agent 구현을 중단할 필요는 없다. |
| G7–G9 복구·안정성·성능 | Snapshot→CLI 복구→HTTP 재개·원본 해시, Worker 재시작/취소/Timeout의 제한된 구현 시험. | 지속 운영 복구·Migration, 선언한 100 Run 안정성·p95 성능·동시 사용자/대용량 부하 목표. 현재 제한값이나 한 번의 Smoke를 합격 수치로 대신하지 않는다. |
| G10–G12 권한·배포권·Release | 원본 불변·로컬 웹 보호·미승인 근거 패키지. | 역할/프로젝트 접근 Matrix, 독립 검토자 승인·철회, SBOM/전이 의존성·원본/결과 배포권 검토, 서명·지원/롤백 정책. 라이선스 문자열 검사는 권리 검토 완료가 아니다. |

## 다음 통합 인도에서 확인할 최소 항목

현재 구현·HTTP·UI·설치 증거가 있는 연결은 **세 제한 Solver, 분광 Lens·선언 PSF의 Native 평면 영상 소비, 보존 RAW의 변경 ISP 처리, 원본까지 이어지는 근거 패키지**다. 각 보고서가 다룬 경로를 회귀 기준으로 보존하고 다음 공백을 닫는다.

1. **현재 범위 재현성과 인도 유지.** 확인한 v0.4 API→실제 Worker→UI→원본 Download/근거 ZIP과 설치 Package를 변경 후에도 재현하고 버전·환경·소스·실제 결과를 기록한다. 다음 운영 증거는 기존 의존성에 기대지 않는 새 설치, 업그레이드/복구와 지속 부하다. 제한된 기능·브라우저 흐름의 통과를 모든 경로의 검증으로 넓히지 않는다.
2. **다음 공간·영상 Gate.** 실제 Lens Wavefront와 시야/거리별 PSF, 깊이·가림·3D 형상/재질/광원으로 확장한다. 연속 소스 Cell 적분과 수신 Pixel 적분을 분리하고, 기존 공간/각도 분리 근사의 오차를 확인한다. 병행하여 SFR/색오차 관측과 임의 ISP Stage/Backend를 추가한다. RAW Replay가 센서 전단 계산이나 재촬영을 검증한 것으로 취급되지 않게 한다.
3. **모델·계측·수치 불확실도 전파와 독립 검증.** 파장/각도 보간·ROI 표본화·공유 Dark의 상관을 포함해 알려진 항목과 unknown을 구분한다. 해석적 기준과 독립 Holdout으로 검증하며, 훈련 자료의 새 ID·재정렬·부분 프레임 재사용도 독립 근거로 인정하지 않는다. 실측 확보와 계산 기능 구현을 병행한다.
4. **시간·제어와 일반 실험 확장.** 기록 설정 Replay→실제 적용 설정/지연→AE/AWB/HDR/LFM·Rolling shutter 순서로 연결하고 실제 RAW/RGB 변화로 확인한다. 민감도·공차·다목적 Trade-off·적응형 추가 해석/실측과 의존 DAG별 캐시를 유지한다.
5. **Factory·Surrogate·운영·Agent 확대.** 일반 Lens/Pixel 구조와 명시적 전하 수집, 계보·유효 범위·오차가 검증된 Surrogate, Agent 계획·비용·도구 정책을 연결한다. 권한/승인·변경 영향·배포권, 깨끗한 설치·Migration·부하·복구 Gate를 계속 구현한다. Collection 근거가 없는 FDTD 결과는 광학 응답으로 보존한다.

이 감사는 전체 Goal이나 상용제품 완료 판정이 아니다. v0.4.0의 제한 공간 계산·ISP 재처리·광학 Factory 기반을 유지하면서, 원문의 일곱 모듈과 네 실행 방식 전체를 다음 인도의 평가 기준으로 삼는다.
