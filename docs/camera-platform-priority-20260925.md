# Camera Simulation & Validation: 우선 개발 범위 (2026-09-25)

## 2026-09-27 재평가: 보유 DB를 사용자 작업 흐름에 먼저 연결

R1 RC2의 계산과 기록은 유효하지만, 여섯 합성 조합을 실제 렌즈·센서 제품
선택지로 해석할 수 없다. 가장 가까운 제품 성과는 새 물리 솔버의 수가 아니라
**기존 revision을 찾아 선택하고, 모델의 의미를 유지한 채 계산·비교·저장하며,
Agent가 같은 경로를 끝까지 실행하는 것**이다. 일반 광학·픽셀·전기 모델,
HDR/LFM, 고급 ISP·최적화는 계속 중요한 다음 확장 순서지만 DB와 평가 연결의
검증을 앞질러 설치본 지원 범위로 승격하지 않는다.

개발 소스 `0.16.2.dev3`의 첫 연결은 제한된 두 트랙이다. 기존 렌즈 DB의
검증된 **기하 PSF**로 두 revision의 중심·주변부 spot과 CRA를 비교하고,
저장 센서 시뮬레이션의 **native 응답·readout**으로 두 revision의 정수 RAW와
광전자 수를 같은 선언 조건에서 비교한다. 출처 해시와 가정·오버라이드를
보존한다. 렌즈 PSF의 절대 광량·센서 샘플링이 검증되지 않아 렌즈 2개×센서
2개의 RAW 결합은 실행하지 않는다. 이 부재를 임의 전송률로 메우면 계산은
되더라도 잘못된 설계 근거가 된다. [DB 비교 계약](camera-platform-db-integration.md)을
따른다. 이 소스 작업은 고정된 RC2 ZIP/wheel의 설치 지원을 바꾸지 않는다.

다음 수용 순서는 (1) 실제 DB revision과 실행 결과의 동일성·원본 보존,
(2) 기존 CameraE2E의 공간·색·과제 metric을 해당 모델 출처와 분리해 연결,
(3) FDTD/TCAD LUT의 조건·정규화·수집 의미가 맞을 때만 교체 전후 RAW,
(4) SDK 제출→공통 대기→전문 조회·저장과 최신 문서,
(5) 대표 해상도·시나리오에서 시간·메모리 확인이다. 서로 다른 렌즈 두 개와
센서 두 개의 **결합된** 동일 장면 RAW·영상·성능 비교는 아직 수용 기준으로
남는다. 진행률은 승인된 전체 분모가 없어 단일 %로 표시하지 않는다.

## 2026-09-27 R1 RC2 이후 최우선 모듈 확장

DB 연결 뒤의 물리 기능 우선순위는 **일반 광학·픽셀·전기 모델 → HDR/LFM →
고급 ISP·최적화**다.
[R1 RC2 설치 검증](../reports/camera_platform/r1-rc2-20260927/installed-proof-release/install-evidence.json)의
14개 통과와 [RC2 manifest](../reports/camera_platform/r1-rc2-20260927/product-release-manifest.json)의
`rc_ready`는 등록된 16×16·500/600 nm **정적 설계 실험**과 단일 자산의 제한된
시간/HDR/DCG 파일럿의 증거다. 정적 경로의 여섯 조합은 부품 ID·revision·hash를
고정하지만, 임의 광학·픽셀·전기 자유 조합이나 PWM/LFM·고급 ISP·최적화를
설치본의 정식 지원으로 승인하지 않는다. 실물 정확도와 제품 승인은 별도다.
[RC2 지원 범위](camera-platform-r1-rc2-support.md)를 기준으로 한다.

1. **범용 모델 결합을 먼저 제품 경로에 연결한다.** Lens의 기준면·광량·위치별
   RI/PSF/입사각과 Pixel의 QE·각도/공간 응답·수집 전하, Electrical의 reset·gain·
   잡음·ADC 계약을 독립 버전 자산으로 로드한다. Composer는 파장·좌표·기준면·
   포함 효과·유효 조건을 검사하고 중복 광량/QE/전하 적용 및 미선언 조건을
   차단하거나 `INCONCLUSIVE`로 남긴다. 두 개 이상의 비예제 모델 계열을 새
   설치본에서 교체해 정수 RAW 차이, 저장 원본, 재열기, 결정 provenance를
   증명한 뒤 지원 범위를 올린다. 현재 R1의 부품 변형은 고정 필드 치환이며
   깊이·왜곡·복잡 회로의 범용 물리 모델을 뜻하지 않는다.
2. **제한된 HDR/PWM-LFM을 등록 사용자 흐름으로 승격한다.** 정적 독립-reset
   bracket부터 Program·Control·PWM 위상·행 노출 창을 명시적으로 선택하게
   하고 각 원본 정수 RAW, 행별 유효시간 경계, fusion 결과와 실패 사유를
   보존한다. 두 위상·두 광량 조건의 독립 수치 대조와 설치본 API/Worker/화면
   저장·재열기를 요구한다. 현재 all-phase ideal PWM 경계와 HDR 계산은
   연구/진단 근거이며 움직임·driver jitter·vendor LFM 성능 주장은 제외한다.
3. **원본 RAW에 고급 ISP와 제약 최적화를 선택형으로 연결한다.** ISP v3
   profile·보정 자산을 버전 고정하고 고정 ISP와 같은 RAW에서 별도 RGB를
   생성한다. 선언된 dynamic ROI의 시간/색 지표와 별도 noise ROI, 목적함수·
   제약·무적합 결과를 저장한다. 후보 선택을 먼저 동결하고 출처가 분리된
   후속 입력으로 평가하며 원본 RAW와 각 후보 RGB를 재열기/재생한다. 설치본
   사용자 경로·실패 복구 검증 뒤에만 지원으로 승격한다. 현재 후속 평가의
   baseline-relative 지표는 실측 화질이나 블라인드 검증이 아니다.

각 단계는 계산/소스 시험, 독립 설치 실행, 교차 도구 대조, 실물 측정을 별도
상태로 기록한다. 기존 R1 wheel·기본 v0.15 상태를 보존하고 새 프로젝트에서
검증한다. 아래 기록은 이 우선순위 설정 이전의 개발 이력이다.

**2026-09-27 개발 소스 `0.16.1.dev32` — 소스 광학을 HDR/LFM·DCG까지 확장하고 ISP 시간 표시 제약 보강:**
선택형 `source_position_ri_v1`을 독립 브래킷 HDR과 공유 전하 HCG/LCG DCG에
연결했다. Native 소스 셀/서브셀에서 Lens RI를 PSF 전에 적용하고, HDR의 PWM·
비변조 성분은 각기 행별 적분 후 전기 RAW와 ISP/HDR에 전달한다. DCG는 한
광전하를 두 readout으로 읽는다. HDR 합성의 정적 조건은 서브셀 변화까지
검사하고, 숫자 광학값이 같은 출처 라벨 차이는 움직임으로 오판하지 않는다.
선택 모드가 없는 ndarray 서브셀의 해시 오류도 수정하면서 기존 JSON 목록
입력의 해시와 모드 생략 결과를 유지했다. SDK·등록 Run·별도 로컬 Worker에서
선택 모드의 정수 RAW 경로를 확인했다.

고급 ISP의 전향적 후보 선택에는 표시 영상의 프레임 변화가 기준 ISP와 얼마나
다른지 확인하는 **선택형 네 번째 지표/상한**을 추가했다. 톤 처리 이후 시간
변화를 평탄화하는 후보는 선언한 상한으로 막고, 기준 출력에 분해 가능한 변화가
없으면 `unknown`이다. 기존 세 목적과 guard는 필수이며, 홀드아웃은 출처 분리·
후등록이지만 공유 호스트 접근 통제에 의한 블라인드 검증은 아니다.

[dev32 개발 근거](../reports/camera_platform/physical-chain-v0161-dev32/evidence-summary.json)에
HDR/DCG·비선형·등록 Worker 교차 **140개**, 시간·잔류 전하·3D 연계 **43개**,
등록 소스/ROI **7개**, ISP 전향적·관측 기반 **19개** 통과와 소스 해시를
기록했다. 범위가 겹치므로 숫자를 합산하지 않는다. 이는 이산 모델의 계산과
로컬 실행 검증이며 독립 실측은 **0건**, 물리 정확도와 전체 예측 불확실도는
미확립이다. `reference_hdr_v1`, retained photo-HDR 등의 소스 RI 계약,
연속 가림 오차 상한, 실측 HDR/LFM 성능, 고객 설치·운영은 남아 있다. 기본
작업실의 마지막 확인 버전은 v0.15.0이고 dev32는 설치하지 않았다. 승인된
전체 상용화 분모와 가중치가 없어 단일 %를 산정하지 않는다.

**2026-09-27 개발 소스 `0.16.1.dev31` — 일반 광학·픽셀·전기와 제한된 HDR/LFM 결합:**
등록형 잔류 전하의 **트랩·HCG/LCG·PWM·이중 광원** 계약에 소스 위치 상대 조도를
연결했다. 각 광원의 native 셀/서브셀과 각 시간 knot에서 RI를 PSF 전에 적용하고,
별도 행 적분 후 한 공유 전하·트랩 상태와 정수 RAW/ISP로 전달한다. 두 실제 3D Light
시퀀스의 별도 Worker 실행, 저장 원본 바이트를 이용한 독립 광량·행 적분, 냉간
변조 거부를 확인했다. 기존 모드 생략 결과는 해시가 유지되며 다른 HDR/DCG topology는
이 모드를 거부한다. [시간 광학 계약](camera-platform-temporal-source-position-ri.md)을
따른다. 정적 경로에서도 소스 RI·비대칭 PSF·수신 각도별 native 생성 전하·인접 well
이동·RAW와 합성 halo ROI를 독립 계산으로 검사했다.

광학 보관본은 원본 Factory `characterization.json`의 절대 광량·RI·입사각·PSF
응답과 고정 조리개·유한 물체 거리를 실제 Lens/Camera에 대조한다. 같은 처방의
옛 응답 배열을 섞으면 거부한다. 기존 clear/quarter/radial 보관본은 유지되지만,
과거 finite-plane HTTP 보관본에는 필요한 Factory 원본 blob이 없어 이 읽기 경로를
통해 로드할 수 없다. [처방 응답 결합 계약](camera-platform-prescription-response.md).
두 3D Light의 중첩 시간 격자 진단은 광원별·PWM·합계의 행 적분 **관측 차이**를
보고한다. 이것은 연속 가림 오차 상한이 아니다.

[dev31 개발 근거](../reports/camera_platform/physical-chain-v0161-dev31/evidence-summary.json)의
장면·시간·잔류 전하 107개와 광학·HDR·ISP 96개가 통과했다. 고급 ISP의 전향적
후속 데이터 평가는 dev30의 제한된 계약을 유지하며 진짜 블라인드·실측 성능 평가는
아니다. 실측 물리 정확도, 고객 설치·운영, 기본 설치본 전환은 미검증이다. 전체
상용화 완료율의 승인된 분모·가중치가 없어 현재 %는 산정하지 않는다.
기본 작업실의 Builder·Releases 화면은 읽기 전용으로 연결을 확인했다. API 문서에는
실행 버전 v0.15.0이 표시되고, 저장된 릴리스 패키지 2건은 독립 실물 검증 부재로
배포 차단 상태다. dev31 설치 근거는 없다.

**2026-09-27 개발 소스 `0.16.1.dev30` — 광학 시간 경로, 실제 3D 광원 성분, 전향적 ISP 평가:**
등록된 `temporal_scene_v1`에서 선택형 소스 위치 상대 조도를 각 3D 장면 knot의
PSF 전에 적용한다. 같은 소스 격자의 파장별 지도와 knot별 전후 광량을 보존하고,
행별 적분→정수 RAW→ISP까지 연결했다. 기존 모드의 일반 셀·JSON 서브셀 결과
해시도 회귀로 고정했다. 정적 `response_chain`은 실제 연산 순서를 기록하고
소스 모드·서브셀 바이트·crop 위치·파장·입력 출처를 식별자에 묶는다.
[시간 소스 조도 계약](camera-platform-temporal-source-position-ri.md) ·
[정적 체인](camera-platform-response-chain.md)을 따른다.

같은 이동 3D 장면의 두 Light를 별도로 실제 렌더링해 PWM 성분과 주변광 성분으로
쓸 수 있게 했다. 두 시퀀스가 모두 등록 3D 렌더이면 Run 사전검사가 장면·광원·
카메라·시각의 완전한 쌍인지 다시 확인하고, 중복 광원 입력을 거부한다. 등록
실험은 두 3D 입력→행별 PWM/주변광 적분→트랩→공유 전하 HCG/LCG 정수 RAW를
확인했다. 선택한 ROI에서 주변광의 시간 변화는 없었으므로 두 광원 모두의
이동 가림을 증명한 사례는 아니다. [3D 광원 성분 계약](camera-platform-dual-3d-light-components.md).

ISP는 개발 Joint Study에서 후보를 먼저 고정하고, **그 뒤 등록한** 원본 RAW·
시간 참조·Joint Study에서 기준과 고정 후보만 평가하는 전향적 경로를 추가했다.
프레임 픽셀 복제와 출처 중복을 거부한다. 공유 호스트의 접근 통제가 없으므로
블라인드 평가라고 주장하지 않으며, 결과는 상대 지표만 다룬다.
[전향적 ISP 계약](camera-platform-prospective-isp-optimizer.md)을 따른다.

[dev30 개발 근거](../reports/camera_platform/physical-chain-v0161-dev30/evidence-summary.json)에
소스 해시와 범위를 묶었다. 통합 장면·시간·잔류 전하 **103개**, HDR·ISP 교차
**81개**, 전향적 선택→후속 홀드아웃 등록 Worker **1개**가 각각 통과했다. 시험
집합은 겹치므로 합산하지 않는다. **아직 시간 소스 조도가 HDR/DCG/트랩 경로에
결합되지 않았고**, 실측 물리 예측 검증·기본 설치본 전환·고객 출시 검증도 없다.
다음 핵심 작업은 이 연산 경로를 하나로 묶고, 독립 실측 Lens·픽셀·전기·PWM·
트랩 자료와 조건별 불확실도로 RAW·영상 예측을 평가하는 것이다. 전체 상용화
완료율은 승인된 분모가 없어 산정하지 않는다.

**2026-09-27 개발 소스 `0.16.1.dev29` — 일반 광학·픽셀·전기, HDR/LFM, 고급 ISP·최적화 우선:**
정적 `spatial_scene_v1`에 선택형 `source_position_ri_v1`을 연결했다. Lens의
파장별 상대 조도를 native 소스 셀 또는 보존 서브셀 위치에서 PSF 전에 적용하고,
원본 분광 source·적용 지도·전후 광량 장부를 각각 보존한다. 비대칭 PSF와
4-site 픽셀 전자량·정수 RAW는 독립 직접 합산으로 검사했다. 기존 기본 연산의
배열·summary 해시는 그대로 유지한다. 비중심 capture ROI의 보존 서브셀 field
PSF가 수신 crop 원점을 소스 원점으로 잘못 쓰던 버그도 고쳐 전체 frame
직접 합산과 RAW를 대조했다. 이는 이산 광학 연산의 수정이며 Lens–PSF–입사각–
픽셀 수집을 완전히 결합한 실측 물리 모델은 아니다.
[소스 상대 조도 계약](camera-platform-source-position-ri.md)을 따른다.

움직이는 3D 가림의 실제 Mitsuba 렌더 매듭 5개에 작성 PWM LED 파형과
독립 작성 영상면 주변광을 연결해, 행별 적분→트랩을 가진 잔류 전하→공유 전하
HCG/LCG 정수 RAW→냉간 ISP 재처리·번들까지 등록 실행했다. 위상 변경은 LED
적분과 RAW를 바꾸고 주변광 전자량은 그대로 유지한다. 별도 스칼라 시간 적분과
트랩·ADC 계산을 대조했다. 주변광은 **두 번째 3D 물리 광원 렌더가 아니며**,
매듭 사이 연속 가림 오차와 실측 LFM 성능은 알 수 없다.
[결합 실험 계약](camera-platform-registered-3d-pwm-trapped.md)을 따른다.

고급 ISP·최적화의 실측 입력 경계를 위해, 원본 nonlinear RGGB Capture와
완료된 retained Run의 정수 RAW를 같은 native ROI에서 읽기 전용으로 비교하는
API/SDK를 추가했다. 형식·Test·기하·적용 조건이 맞을 때만 기술적 DN 잔차를
내고, 불명확한 적용 설정은 `INCONCLUSIVE`로 수치 지표를 막는다. 실측이라는
메타데이터만으로 시간 ISP·HDR·최적화 성능이나 물리적 PASS를 만들지 않는다.
[RAW 비교 계약](camera-platform-retained-raw-capture-comparison.md)을 따른다.

**현재 검증 및 남은 순서:** 광학 순수 회귀 157개, 광학 등록 경로 18개,
실측 RAW 비교 경로 6개, 결합 3D/PWM/트랩 실험 2개와 전기·CDS·픽셀·전하
수집 회귀 244개가 통과했다. 각 시험 묶음은 겹치므로 수를 합산하지 않는다.
신규 경로를 포함한 최종 교차 회귀도 **122개 통과**했다.
[dev29 개발 근거](../reports/camera_platform/physical-chain-v0161-dev29/evidence-summary.json)에
검사 범위와 소스 해시를 기록한다. 다음은 (1) 시간/HDR 경로에 소스 위치 광학
모드와 실제 이중 3D 광원을 조건·출처를 보존해 연결하고, (2) Lens·픽셀·전기·
PWM·트랩의 독립 실측과 불확실도를 확보하며, (3) 그 데이터에서 HDR/LFM·ISP
선택을 독립 평가하는 것이다. 기본 설치본으로의 전환과 고객 출시 검증은
별도 단계다. 승인된 전체 분모·가중치가 없어 상용화 %는 산정하지 않는다.

**2026-09-27 개발 소스 `0.16.1.dev28` — 시간 샘플링 3D, RAW 움직임 HDR, 잔류 전하 ROI, 관측 기반 ISP 선택:**
움직이는 3D Scene을 최대 65개 상대 ns 시각에서 실제로 다시 렌더링하고, 동일
장면·clock·Lens·카메라의 완료 렌더만 하나의 분광 시퀀스로 게시한다. 거친 격자와
중첩된 촘촘한 격자의 조사도 적분 차이를 비교할 수 있다. 행 노출 창을 넘기면
창별 차이도 계산하지만, 창은 호출자가 제공하며 등록 Control/Run에 자동 결합되지
않는다. 격자 차이는 **관측된 수치 변화**이지 연속 가림 오차의 상한이 아니다.
[3D 시간 계약](camera-platform-sampled-3d-motion.md)을 따른다.

`reference_hdr_v1`에는 일정 광원·global shutter의 정수 RAW만으로 짝수 픽셀
전역 이동을 추정하는 선택형 합성을 추가했다. 모호한 이동·국소 불일치·기준
노출 clipping에서는 혼합을 막거나 기준값을 보존한다. 12×12 수신 ROI의 실제
Mitsuba 이동 장면을 Test→SDK→Worker→결과 묶음까지 연결했고 native 수신 위치를
근거에 남긴다. 이는 국소 가림을 해결했다는 뜻이 아니다. 기존 HDR 두 광원
사전 검사에서 주변광 측 프로그램 정보가 빠져 HTTP 422가 나던 문제도 고쳤다.
[RAW 정합 계약](camera-platform-reference-hdr-translation.md)을 따른다.

잔류 전하 v1–v4는 하나의 고정 native 수신 ROI에서 행 시각·CFA 위상·전하
상태의 원점과 해시를 유지한다. 단일 조명과 PWM LED+주변광, 일반·트랩·두 gain
경로를 등록 Worker와 냉간 ISP 재처리까지 검사한다. 전체 프레임과 ROI의 결정론적
전자 수·고정 패턴은 대응하지만 같은 seed의 잡음 픽셀값은 동일하지 않다. 명시적
`capture_crop` 없는 부분 소스는 바깥을 암영으로 가정하므로 전체 프레임과의
광학 halo 동등성을 주장할 수 없다. v5 역산형 photo-HDR ROI는 계속 차단한다.
[전하 계약](camera-platform-retained-charge.md)을 따른다.

관측 기반 ISP 최적화는 완료된 두 Joint Study v3에서 개발 선택을 별도 artifact로
고정하고, 출처가 다른 평가 입력에서 같은 후보와 기준 ISP를 비교한다. 저장된
RAW/ISP 출력으로 잡음·시간 변화·색 이탈 제한을 점수화하지만, Joint Study의
실측 아닌 작성 시간 참조를 냉간 검증하는 경로에는 의존한다. 전체 평가 후보를
검증한 뒤 두 프로파일만 점수화하며, 이미 완료된 평가 결과를 사용자가 미리 볼 수
있으므로 **블라인드가 아닌 출처 분리형 사후 평가**다.
[최적화 계약](camera-platform-observable-isp-optimizer.md)을 따른다.

**제품 경계:** 이 단계는 소프트웨어 계산과 로컬 등록 Worker 검증이다. 독립
실측 Lens·픽셀·전기·PWM 데이터와 물리 정확도 검증은 0건이며 실제 HDR/LFM
성능, 고객 설치, 다중 사용자 성능·출시 적격성은 미확정이다. 개발 소스는 기본
설치본으로 전환하지 않았고 패키징 버전은 별도 `pyisetcam 0.1.0`이다. 상용화
전체 %는 승인된 분모와 가중치가 없어 산정하지 않는다.
[dev28 개발 근거](../reports/camera_platform/physical-chain-v0161-dev28/evidence-summary.json)에
검증 범위와 남은 경계를 기록한다.

**2026-09-27 개발 소스 `0.16.1.dev27` — 서브셀→PSF, 트랩 두 광원, CDS, 최종 ISP 출력:**
선택형 3D 렌더가 native 소스 셀의 서브셀 분광 광휘를 평균 전 불변 배열로
보관한다. 렌더·저장 광휘 재투영·게시·Test·Worker RAW/ISP는 이 배열에 Lens
절대 광량을 한 번 적용한 뒤, 각 서브셀 위치의 수신 픽셀 적분 PSF로 전달한다.
넓은 31×31 PSF halo도 사전 자원 제한 안에서 FFT로 계산하고, 독립 직접 합산과
입력·센서 안팎 광량 ledger를 대조했다. 현재 분수 위치 응답은 **기존 중심
impulse의 2차원 선형 보간 근사**이며 실측 연속 PSF가 아니다.
[서브셀 광학 계약](camera-platform-subcell-psf-transfer.md)을 따른다.

잔류 전하 v2/v4도 PWM LED와 비변조 주변광을 각 scene knot·PWM 경계·트랩
substep에서 따로 적분해 하나의 자유 전하·트랩 상태로 갱신한다. 두 개의 완성된
RAW를 합치지 않으며 Test·Worker·ISP 출처와 트랩 시간 계획을 재검사한다.
v5 photo-HDR 역산과 부분 native 잔류 전하 ROI는 계속 거부한다.
[두 광원 계약](camera-platform-retained-two-component.md)을 따른다. 별도
`independent_bracket_cds` 프로그램은 reset/signal 샘플과 native 행·열 상관
잡음으로 정수 RAW·ISP까지 간다. 기존 aggregate read-noise와의 이중 계산 및
독립 픽셀 분산만 가정한 HDR fusion은 차단한다.
[CDS 계약](camera-platform-cds-correlated-readout.md)을 따른다.

공동 ISP `result_v3`는 후보별 중간 RGB·시간 gate와 실제 최종 display RGB를
보관한다. 원본 RAW·작성 시간 참조로 지표를 다시 계산하고, 최종 RGB는 고정
tone 전달과 픽셀별로 대조한다. 요약 파일을 함께 다시 작성하더라도 잡음 지표
정의·불확실도·물리 검증 상태를 바꿔치기할 수 없도록 묶는다. 지표 기준면은
여전히 pre-tone 코드 RGB이며 실물 영상 품질을 인증하지 않는다.
[공동 ISP 계약](camera-platform-joint-isp-study.md)을 따른다.

**제품 경계:** 위 기능은 작성된 합성 입력과 등록 Worker의 계산 검증이다.
최종 교차 모듈 회귀 123개와 변경 파일 Ruff 검사가 통과했다.
서브셀 위치별 실측 PSF, trap/CDS/PWM 교정, 독립 측정 시간 참조, 연속 3D
가림·움직임 HDR 정합, 모델 불확실도와 고객 설치·릴리스 자격은 아직 없다.
기본 설치본은 별도 v0.15.0이다. 상용화 전체 %는 승인된 분모·가중치가 없어
산정하지 않는다. [개발 근거](../reports/camera_platform/physical-chain-v0161-dev27/evidence-summary.json)에
검증 범위와 제한을 기록한다.

**2026-09-27 개발 소스 `0.16.1.dev26` — 시간 3D·광학 재투영·두 광원 LFM·ISP 출력 근거:**
일반 광학·픽셀·전기 모델을 연결한 기존 경로 위에, 물체와 카메라의 명시적
월드 좌표 평행 이동을 상대 ns 시각별 3D 렌더로 추가했다. 발행된 렌더와 저장
광휘 재투영 영상의 시계·시각을 등록 scene knot에 정확히 묶고, 실제 3D 가림
변화→행별 노출 적분→HDR Worker→8×8 정수 RAW/RGB까지 실행했다. 행별 평균
전자 수는 별도 스칼라 적분으로 대조했다. 저장된 서브셀 격자 광휘도 원래의
격자·seed·좌표·가중치가 맞을 때 재투영해 다른 Lens 절대 광량 조건에서
재사용할 수 있다. 이 재투영은 **셀 평균**만 사용하며 서브셀 위치별 PSF
적분이나 매듭 사이의 연속 3D 가림 계산은 아니다.

잔류 전하 LFM은 PWM LED와 비변조 주변광을 별도 분광 시퀀스로 받아 각각
적분하고, 합산한 광전하를 한 번의 PD 상태 전이에 넣는다. trap 없는 v1과
동일 전달 전하 HCG/LCG v3의 등록 Test·SDK·Worker·ISP RAW 출처 검증까지
연결했다. 기존 단일 장면 요청의 실행 ID는 유지한다. trap v2/v4와 광전자율
역산 photo-HDR v5는 두 광원 입력을 거부한다. [두 광원 계약](camera-platform-retained-two-component.md)을
따른다. ISP 공동 비교 `result_v2`는 후보별 처리 RGB·시간 gate NPY를 보관하고,
재실행 없이 저장 배열·원본 RAW·등록 시간 참조로 수치 지표와 순위를 다시 검증한다.
기존 v1 결과는 구조 검증만 가능하다. [시간 3D 계약](camera-platform-sampled-3d-motion.md),
[광학 샘플링 계약](camera-platform-source-cell-quadrature.md),
[공동 ISP 계약](camera-platform-joint-isp-study.md)을 따른다.

**제품 경계:** 이 기능들은 작성된 합성 입력의 계산·등록 Worker 검증이다.
실물 Lens·Pixel·전기/PWM 교정, 연속 서브셀→PSF 전달, 정확한 연속 가림 적분,
움직임 HDR 정합·ghost, trap+두 광원, 부분 native 잔류 전하 ROI, 독립 계측
시간 참조와 예측 불확실도는 아직 미확립이다. 기본 설치본은 별도 v0.15.0이며
`dev26`은 설치·고객 릴리스 자격을 얻지 않았다. 독립 실측 0건과 출시 적격성
미확립 상태를 단일 상용화 진행률 숫자로 변환하지 않는다.
[개발 근거](../reports/camera_platform/physical-chain-v0161-dev26/evidence-summary.json)에
검증 범위와 남은 조건을 기록한다.

**2026-09-27 개발 소스 `0.16.1.dev25` — 일반 광학·픽셀·전기, DCG ROI, 공동 ISP 판단:**
같은 3D 가림 장면·분광 광원을 두 개의 Factory Lens/Assembly로 촬영해, 선언된
F/8→F/4 조리개 변화가 광량(이 시험의 근축식에서 4배)과 PSF를 각각 바꾸고
픽셀 전하·정수 RAW·RGB까지 전달되는 것을 확인했다. 등록형 HDR Worker에는
발행한 3D 장면의 방향성 Lens·처방 PSF·native 4-site 생성·비대칭 전하 수집을
함께 넣어 8×8 RAW/RGB와 생성·수집·손실 전하 ledger를 확인했다. 등록형
`shared_charge_dcg_v1`도 충분한 합성 halo를 둔 수신 ROI에서 HCG/LCG 두
정수 RAW, 원래 센서 좌표의 CFA·행 시각·평균 전하·DSNU를 확인했다. 잡음
추출 순서가 다른 전체 frame과 ROI의 정수 RAW가 픽셀별로 같다고 주장하지 않는다.

고급 ISP 비교는 같은 후보 프로파일을 독립 seed의 정지 원본 RAW 4–16건과
별도 동적 원본 RAW에 실제로 적용한다. 정지 반복 잡음과 등록된 시간 참조 대비
신호 변화 오차를 필수 Pareto 목적으로 계산하고, 검증 불가능한 후보는 제외한다.
계획 해시·원본·참조 재검사·Worker·API/SDK·결과 번들을 연결했다. 후보 처리
배열은 별도 보관하지 않으므로 재생산에는 동결된 원본·모델·실행 환경이 필요하다.
실제 발행한 3D 정지 장면의 네 별도 잡음 취득에서도 정지 잡음·시간 참조 지원
상태가 모두 유효한 후보 사례를 확인했다. 시간 참조 자체는 작성한 합성 데이터다.
[등록 ROI 계약](camera-platform-registered-temporal-roi.md),
[공동 ISP 계약](camera-platform-joint-isp-study.md),
[실행 근거](../reports/camera_platform/physical-chain-v0161-dev25/evidence-summary.json)를 따른다.

**남은 핵심 경계:** 장면 서브셀 위치별 연속 PSF 결합과 실제 Lens·Pixel·전기
교정, 실측 PWM/LFM·시간 참조 및 예측 불확실도는 없다. 등록형 잔류 전하 HDR의
수신 ROI, 움직이는 3D 장면의 노출 적분과 일반 HDR 정합·ghost도 미완성이다.
기본 설치본은 여전히 v0.15.0이며 개발 소스의 합성 Worker 검증을 제품 설치나
실물 카메라 성능 보증으로 승격하지 않는다.

**2026-09-27 개발 소스 `0.16.1.dev24` — 일반 광학·전기 경계와 등록형 HDR/LFM ROI:**
3D 소스 셀 면적 샘플링에 선택형 8×8/16×16 격자를 추가하고 독립 면적 해와
비대칭 PSF 직접 합산을 대조했다. 30% 밝은 경계의 16×16 잔여 오차는 +1.25%p이며
격자 증가에 따른 오차는 단조롭지 않다. 선형 전기 모델도 온도 의존 암전류와
고정 seed DSNU의 실제 native 배열 상한을 렌더 전에 검사한다. 등록된 일반 시간
장면과 `hdr_bracket_v1`은 원본 분광 source를 유지한 채 작은 수신 `capture_crop`을
Test·Worker에 동결하고, 두 성분의 출처와 광학·전하 halo를 확인한다.
실제 3D 광학 halo 렌더를 발행한 뒤 등록 HDR Worker의 8×8 RAW/RGB까지 실행했다.
부분 ROI의 선형 비교는 외부 측정이 Test의 수신 ROI·소스 좌표를 선언하지 않거나
다르게 선언하면 무효로 판정한다.
[광학 샘플링 계약](camera-platform-source-cell-quadrature.md),
[등록 ROI 계약](camera-platform-registered-temporal-roi.md),
[실행 근거](../reports/camera_platform/physical-chain-v0161-dev24/evidence-summary.json)를 따른다.
이 결과는 dev24 당시 개발 소스의 합성 계산 검증이다. 당시 연속 서브셀→PSF 결합,
실측 광학·픽셀·전기 정확도, 등록형 **PSF+전하 수집 합성 halo**의 실제 렌더
발행 종단 시험, 기본 앱 설치·제품 출시 검증은 남아 있었다. 합성 halo 종단
시험은 위 dev25에서 추가됐지만 나머지 물리·제품 경계는 계속 열린 상태다.

**다음 구현 순서:** (1) 독립 광학 전달 기준과 실측 Lens·Pixel 조건 자료로
파장·시야·초점·조리개·온도별 적용 범위를 확인하고 서브셀 위치별 PSF 적분을
연결한다. (2) 등록형 DCG·잔류 전하 HDR에도 수신 ROI와 행 시간·공통 전하/경로별
잡음·포화를 결합하고 실측 PWM/LFM의 신호 보존과 실패를 검증한다. (3) ISP·최적화는
독립 시간 참조와 계측 불확실도를 확보해 ghost·지연·잡음·포화의 공동 제약을 평가한다.
계산 검증과 실측 성능 주장은 각각 별도로 표기한다.

**2026-09-27 개발 소스 `0.16.1.dev23` — 광학 경계·이중 HDR 근거·시간 ISP 비교:**
3D 장면 렌더에 선택형 2×2/4×4 native 소스 셀 midpoint 면적 샘플링을 추가하고,
경로 상한·샘플 순서·잔여 격자 오차를 명시했다. 이중 장면 HDR의 두 번째 분광
원본도 등록 Test, artifact, 클록과 고정 색 처리 비교에 묶는다. 작성한 시간별 RGB
참조를 별도 Test로 동결해 ISP 후보의 실제 시간 변화 오차·진폭·지연·잔상·플리커를
평가하고, 불명확한 후보를 Pareto에서 제외한다. [광학 경계 계약](camera-platform-source-cell-quadrature.md),
[시간 참조 계약](camera-platform-temporal-reference.md) 및
[실행 근거](../reports/camera_platform/physical-chain-v0161-dev23/evidence-summary.json)를 따른다.
참조 독립성은 작성자 선언이고 실측 인증이 아니다. 기본 앱 설치본은 이 개발
소스로 교체하지 않았다. dev23 당시 등록형 HDR spectral import는 `capture_crop`을
거부했으므로 부분 소스 이중 HDR의 등록→ISP 종단 경로는 지원 주장 대상이 아니었다.

**dev23 당시 다음 구현 순서:** (1) 광학 소스 면적 적분과 PSF·픽셀 개구의 결합을 독립
수치해 및 격자 수렴성으로 확인하고, 일반 Lens·Pixel의 온도·초점·입사각
조건을 동결 자산으로 연결한다. (2) 등록형 이중 성분 HDR/LFM에 부분 소스
`capture_crop`·합성 halo·행 시각을 종단 연결하고, 전기 mode·온도·gain
계약과 포화·잔류 전하를 함께 검증한다. (3) 고급 ISP·최적화는 물리 계측으로
독립성이 확인된 시간 참조를 추가한 뒤 시간 신호 보존·지연·ghost·잡음·
포화 제약을 함께 평가한다. 세 단계 모두 계산 검증과 실측 정확도를 별도로
표시하며, 개발 소스 검사를 제품 출시 검증으로 승격하지 않는다.

**2026-09-27 개발 소스 `0.16.1.dev22` — 일반 픽셀·광학 전달과 HDR/LFM 우선:**
수신 ROI에 필요한 Lens PSF와 Sensor 전하 이동의 **합성 halo**를 계산하여 부분 native
분광 source의 생성·수집→정수 RAW→ISP를 연결했다. 렌더의 Sensor ID·모델·수집 artifact와
수신 ROI를 고정하고, 실제 Factory 렌즈→Mitsuba 렌더→출판→8×8 RAW/ISP를 실행했다.
주변광처럼 변조되지 않는 분광 영상과 PWM 분광 영상을 별도 불변 시퀀스로 등록해
행별 광전자 적분·전기 RAW·제한된 HDR에 연결했다. 기존 단일 장면 결과의 해시 호환을
포함한 통합 검사 **369개 통과**, Ruff 통과. [계산·실행 근거](../reports/camera_platform/physical-chain-v0161-dev22/evidence-summary.json)와
[두 광원 계약](camera-platform-illumination-components.md)을 따른다. 개발 소스의
합성 계산 검증이며 기본 설치본 또는 실물 정확도 승격은 아니다.

**dev22 당시 확인한 다음 우선순위:** 일반 광학의 날카로운 가림 경계는 현재 픽셀 중심 광선 하나로
표현되므로 spp만 늘려도 서브픽셀 오차가 줄지 않는다. 소스 셀 평균만 추가하면 기존
수신 개구 적분 PSF와 결합해 과도한 흐림을 낼 수 있어, 분수 픽셀 광학 응답 계약과
함께 검증해야 한다. 고급 ISP·최적화의 시간 왜곡 지표는 아직 독립 시간 참값이 아닌
동일 RAW의 무필터 결과 대비 값이다. 다음 수직 슬라이스는 시간·ROI·출처를 고정한
독립 참값 Test에서 신호 진폭·지연·잔상·플리커를 평가하고, 참값이 없는 후보를
Pareto 성능 판정에서 제외하는 것이다. 실제 교정·온도/모드별 적용 범위·고객 설치
검증은 계속 남는다.

**2026-09-27 정적 물리 경로·HDR/PWM 추가:** 개발 소스 `0.16.1.dev21`에서
고정 Lens PSF가 요구하는 장면 렌더 소스 halo를 수신 ROI 바깥에 실제로 생성하고,
전체 센서 입력일 때 픽셀 생성·인접 전하 수집 후 수신 ROI를 잘라 RAW까지 연결했다.
독립 계산으로 방사휘도→PSF→전하 생성·수집→정수 RAW→기본 ISP를 대조했다.
잔류 PD HDR은 작성 PWM의 행별 광적분 시간으로 광전자율을 추정하고 암전하는
실제 전체 시간으로 차감한다. 정적/장면 검사 133개와 시간/HDR 검사 95개가 통과했다.
PSF halo만 있는 부분 장면과 전하 수집의 결합, 실측 PWM/렌즈/픽셀 정확도,
움직임·trap HDR 및 제품 출시 자격은 아직 확립되지 않았다.
[계산·실행 근거](../reports/camera_platform/physical-path-pwm-v0161/evidence-summary.json)를 따른다.

**2026-09-27 최신 — 새 입력 실험의 실제 실행 완료:** 두 소수 후보ID 충돌/최종저장
실패시 근거 유실 결함을 수정했고 관련38시험이 통과했다. 별도 개발데이터에서
화면 제출→실제 Worker5회취득→2 ms선택→별도평가→원본NPY저장/재열기를 완료했다.
실제 Chromium17항목 통과, 새 실험POST1회이며 조회로 재계산하지 않는다.
[실행 기능과 저장 결과 열기](camera-platform-design-experiments.md) 및
[최신 상태](../reports/camera_platform/progress-20260927.md)를 먼저 따른다.
다음은 같은 입력 작업실에 명시적인 움직임/채널비 비용을 연결하는 기능 확장이다.
아래 완료 연구와 이번 정상연결smoke를 반복하는 것으로 진척을 대신하지 않는다.

**2026-09-26 최신 — 저장 결정 작업실:** 신호 우선 radial·4 ms와 움직임/채널비
우선 clear·2 ms의 기준·실패·원본 RAW·출처를 읽기 전용 화면으로 연결했다.
결과 139개를 읽되 새 취득/재선택은 하지 않는다. 기존 앱 개발 소스의 실험·비교 진입과
DB 없는 독립 viewer를 제공한다. 시험 10개, 실제 브라우저 15항목이 통과했다.
[사용법과 적용 경계](camera-platform-decision-review.md) 및
[최신 진행](../reports/camera_platform/progress-20260926.md)을 따른다.
다음은 또 다른 읽기 전용 요약 대신 기존 계산기를 재사용한 명시적 새 실험 실행 흐름이다.

**2026-09-26 최신 — 움직임/채널비 추가 결정:** 이전 radial·4 ms와 clear·2 ms를
그대로 비교했다. 8픽셀 주기·1,000 pixel/s에서 정적 대비는 63.662%/90.032%였고,
neutral 고정 보정 후 radial의 새 분광 입력 채널비 잔차는 5.381%/5.915%였다.
추가 연구 기준에서는 clear·2 ms·gain 1을 선택했고, 고정 후 새 입력 3개 통과,
속도 스트레스 2개 실패를 보존했다. 이전 signal-only 결정은 삭제하지 않았다.
global box shutter·고정 창·두 파장·잡음 OFF 연구이며 실물/일반 인지 검증이 아니다.
[움직임·채널비 계약](camera-platform-motion-color-tradeoff.md)과
[최신 진행](../reports/camera_platform/progress-20260926.md)을 먼저 따른다.
다음은 저장된 두 결정을 기존 실험 작업실에 연결하는 일이며 동일 연구 반복이 아니다.

**이전 선택 결과:** 최소 신호·예측 SNR·well/RAW 포화·노출 상한을
동시에 적용한 36점 중 5점이 적합하고 radial·4 ms·gain1이 선택됐다.
선택 고정 후 별도 미러/회전/스펙트럼 입력과 새 seed 15/15에서 기준을 유지했다.
범위 밖 국부 강조광 5/5에서는 포화 한계를 넘어 확대 사용은 승인하지 않았다.
[제약 선택 계약](camera-platform-constrained-selection.md)과
[최신 진행](../reports/camera_platform/progress-20260926.md)을 따른다.
이하 결과는 이전 단계 이력이며 현재 완료 과제로 재수행하지 않는다.

**2026-09-26 최신:** 기존 동일 처방의 clear/quarter/radial 자산을 공통 응답 경로에
연결한 42개 전기 취득을 완료했다. 광량 손실의 gain/노출 보상은 같은 밝기라도 전하·잡음·
포화 위치·색 비율이 다르다. 새 광학 추적/물리 검증으로 집계하지 않는다.
[처방 연결과 설계 판단](camera-platform-prescription-response.md) 및
[최신 진행](../reports/camera_platform/progress-20260926.md)을 먼저 따른다.
다음은 최소 신호·포화·노출 제약을 함께 갖는 후보 선택과 별도 hold-out 광입력 평가다.

**이전 응답 비교:** 이상 동공 F/4·F/8, 픽셀 각도 응답 2개, gain 1/2,
분광조도 2개를 실제 PSF→전하→RAW로 연결한 16조건 비교를 완료했다.
광전하 증가와 포화 증가, F-number의 광량/회절 상충을 수치로 확인했다.
[응답 비교 구현](camera-platform-response-chain.md)과
[현재 설계 결론](../reports/camera_platform/response-comparison-20260926/interpretation.md)을 따른다.
당시 다음 과제였던 기존 처방 Factory 조건 연결은 위 최신 단계에서 완료했다.

**2026-09-26 최신 연결:** 선택형 RAW 시간 필터를 기존 원본·실제 취득 시간 검증,
별도 Worker 작업, 원본/처리/마스크 비교 화면까지 연결했다. 저장 결과 재열기도 확인했다.
기본 ISP와 기본 앱은 변경하지 않았고 실제 주기 신호 손실 경고를 유지한다.
[새 개발 작업실 사용법](camera-platform-raw-temporal-replay.md)을 참고한다.
이 연결은 완료로 두고 다음 개발은 일반 광학/픽셀/전기 응답의 두 조건 비교로 돌아간다.

**2026-09-26 최신 결과:** 50 fps를 유지하는 선택형 RAW 필터를 구현하고 큰 움직임·
광량 변화에서 단순 평균보다 추가 잔상이 작은 것을 확인했다. 정적 후반 변동은
잡음 OFF 4.06881% → 0%지만 실제 2프레임 주기 장면도 평탄화되는 반례가 있어
기본 활성화하지 않는다. 평균된 영역의 명목 정보 나이는 10 ms다.
[필터 계약·사용법](camera-platform-fixed-rate-raw-filter.md)과
[최신 진행](../reports/camera_platform/progress-20260926.md)을 기준으로 이어간다.

**2026-09-26 후속 결과:** gain 1.0의 연속 16프레임 연구에서 남은 변동이
trap/PWM/촬영 위상의 결합임을 분리했다. 20 ms 간격의 후반 최대 상대 변동
4.06881%는 24 ms에서 잡음 OFF 기준 0%였지만, 50 → 41.667 fps의 대가가 있다.
50 fps 유지의 해결이나 밝기 목표 달성으로 해석하지 않는다.
[현재 원인·대안 비교](../reports/camera_platform/pwm-transient-20260926/interpretation.md)와
[현재 진행·다음 작업](../reports/camera_platform/progress-20260926.md)을 따른다.

**2026-09-26 인수 작업:** 실제 AE 설정을 초기 계획과 같게 요구하던 후보 계획기 결함을
수정했다. 원래 8×8·8 ms/4 ms PWM 조건을 보존한 직접 Runtime 연구에서 gain 0.4/0.42는
각 64개 관측 프레임 모두 목표 DN을 만족했지만 상대 시간 변동과 gain 환산 잡음은
개선되지 않았다. 원래 등록 모델 범위와 기본 앱은 변경하지 않았다.
[최신 결과·범위·다음 결정](../reports/camera_platform/progress-20260926.md)을 먼저 참고한다.
아래 내용은 dev19까지의 개발 이력이며 최신 완료 상태는 위 결과와 구분한다.

원문의 7개 모듈(Registry, Model Factory, Composer, Runtime, Validation, Experiment/Optimization, Agent/Release)과 4개 실행 모드는 유지한다. 다음 개발은 **일반 광학·픽셀·전기 모델 → HDR/LFM → 고급 ISP·최적화**에 집중한다. 현재 구현을 실물 정확도 또는 판매 가능한 성능 보증으로 해석하지 않는다.

| 순서 | 개발 결과물 | 완료 판정 |
|---|---|---|
| 1. 일반 모델 | 렌즈의 파장·상고·초점·조리개별 PSF/광량/CRA와 센서의 파장·각도·픽셀 구조별 QE/누화, 전기 gain·noise·full-well·readout mode를 **조건/버전/출처가 있는 응답 자산**으로 묶는다. | Composer가 공통 조건 교집합과 적용 범위 밖 사용을 판정하고, Factory 결과에서 RAW까지 원본 계보를 추적한다. 최소 2개 조건에서 독립 계산 또는 실측 대조를 남긴다. |
| 2. HDR/LFM | 실제 프레임·row 타이밍, PWM 위상, trap/잔류 상태, 단일·HCG/LCG·다중 노출 경로의 관측 RAW를 같은 시험 계약에서 비교한다. | 유효 exposure/gain/phase 후보를 미리 검사하고, 원본 RAW·rail·노이즈·잔류 상태·실제 적용값을 저장한다. HDR/LFM 성능 승격은 독립 기준 촬영과 불확실도 이후에만 허용한다. |
| 3. 고급 ISP·최적화 | 원본 RAW에서 시작하는 ISP graph와 중간 출력, 실제 파라미터 적용 증거를 보존한 제약 Pareto 탐색을 연결한다. | 후보마다 동일 시험/모델 버전을 강제하고, 재현 가능한 비교·실패 이유·다음 실험을 기록한다. 독립 검증 없는 후보를 자동 승인하지 않는다. |

현재 개발 소스에는 [native 4사이트 응답](camera-platform-native-pixels.md), [전기 모델·제어](camera-platform-electrical-control.md), [RAW 기반 고급 ISP](camera-platform-advanced-isp.md), [동일 RAW ISP 후보 연구](camera-platform-isp-studies.md)와 제한된 HDR/LFM 경로가 있다. 이들은 서로 다른 적용 조건과 검증 수준을 갖는다. 다음 작업은 기능 목록을 늘리는 것보다 **조건별 일반 모델의 근거를 채우고 동일한 동결 Test에서 연결되는지 확인**하는 데 둔다. 특히 gain을 Lens·Sensor·ISP 공통 스칼라로만 취급하는 현 계약은 아날로그 readout gain, 디지털 gain, ISP gain의 위치와 효과를 구분하지 못한다. 새 gain 후보를 실행하기 전에 이 의미와 적용 범위를 모델별로 분리해야 한다.

단기 인도 순서는 (1) 파장·field·stop과 픽셀 사이트·입사 방향·전기 모드가 만나는 응답 계약 및 독립 조건 검증, (2) 그 계약을 동결한 HDR/LFM 노출·gain·위상 후보의 실제 RAW 비교, (3) 동일 RAW 또는 동등 조건의 고급 ISP 후보 비교와 제한된 Pareto 탐색이다. 각 단계의 합성 계산 검증과 실측 검증을 별도 표시한다. 과거 `*-next-slice.md` 문서는 작성 시점의 제안이므로, 현재 제공 여부는 위 구현 문서와 실제 소스·검증 보고서로 판정한다.

## 현재 확인된 설계 문제

작성한 trap/PWM 사례는 정책상 8 ms만 허용되며, 4프레임 원본 RAW 측광은 571.6875/606.75/584.21875/608.875 DN이다. 목표 200–300 DN에 들어간 프레임은 0개다. **원래 Lens·Sensor·ISP 세 모델의 등록된 gain 유효 범위가 모두 1.0–1.0**이므로 0.4/0.5 같은 후보를 같은 자산의 허용 범위 안에서 실행할 수 없다. 이 범위를 조용히 확장하면 적합성 검사와 출처 계약을 무너뜨린다. 우선 필요한 것은 gain의 물리적 의미(아날로그/디지털/ISP)와 자산별 적용 범위를 명확히 하고, 측정 또는 명시적 합성 가정에 근거한 새 모델 버전을 등록하는 것이다. 원래 사례의 밝기 목표 달성 가능성은 여전히 미확립이다.

## 이번 소스 추가: 조건을 맞춘 LFM gain 스크리닝

`POST /api/v1/lfm/gain-screens`는 이미 완료된 2–8개 `electrical_control_v1` Worker run을 읽는다. 각 run은 단일 선형 readout, trapped AE, 허용 노출 한 점, 프레임 전체의 고정 gain이어야 한다. 장면·모델·seed·정책·시간·노출을 고정하고 gain만 달라야 한다. 서비스는 해시 검증한 **원본 정수 RAW**에서 측광을 다시 계산한 뒤 목표 프레임 수, 최악 목표 거리, digital high rail을 저장한다. `GET`과 목록 조회는 원본 RAW 및 결과를 다시 검증한다. 관측 Pareto run ID만 제공하며 자동 선택은 하지 않는다.

이 기능의 시험은 별도로 **합성 gain 범위 0.4–1.0을 명시한 모델 개정판**을 만들어 실제 Worker run 3건으로 수행했다. 이는 원래 1.0 고정 자산의 검증이나 실측 gain 응답이 아니다. 원래 자산의 0.5 후보는 Composer가 거절함을 별도 검사한다. 물리 LFM 성능, well saturation의 부재, 총 예측 불확실도는 아직 확립되지 않았다. 새 소스는 기본 설치본에 배포하지 않았다.

다음 판별 실험은 (1) 실제 허용 gain/mode의 출처와 교정 자료를 확보하거나 명시적 연구용 합성 모델로 분리하고, (2) 동일 PWM 위상·시간·장면에서 각 후보를 실행하며, (3) RAW rail뿐 아니라 전하 full-well, read noise, trap history를 대조하고, (4) 위상·seed·온도 반복과 독립 실측으로 후보의 안정성을 확인하는 것이다. 광학·픽셀 응답의 조건 범위와 ISP graph 적용 버전도 같은 비교 레코드에 고정해야 한다.

## 다음 소스 추가: 모델 조건 공간 진단

`POST /api/v1/assemblies/condition-space`는 저장된 Assembly와 Lens·Sensor·ISP 모델을 읽어 후보 조건별 선언 범위, 모델 간 교집합, 차단 모델 ID와 Composer 결과를 반환한다. 변경이나 자동 범위 확장은 없다. 축 선언 누락은 `unknown_missing_declaration`, 단위 불일치는 `unit_mismatch`, 교집합이 비면 `empty_intersection`이다. 결과의 `compatible`은 **Composer 적합성만** 뜻한다. 특정 Capture Program의 PWM 노출 정책, Worker 실행 가능성, 물리 정확도는 별도 검사 대상이며 `execution_feasibility`는 미확립으로 남는다.

[원래 8 ms PWM 자산 진단](../reports/camera_platform/condition-space-v0161/original-pwm-domain.json)에서 gain 교집합은 1.0–1.0이다. 0.4/0.5는 Lens·Sensor·ISP 세 모델 모두가 거절하고 1.0만 Composer 적합이다. 노출의 **모델 선언 범위**는 0.1–20 ms지만, 원래 Capture Program 정책이 허용하는 노출은 8 ms 하나이므로 4 ms를 실행 가능 후보로 해석하면 안 된다. 진단 전후 QA 레지스트리 파일 해시는 동일했다.

Builder에도 후보값 입력과 모델별 차단 이유를 표시한다. 관련 Python 검사 35개와 최종 데스크톱·모바일 브라우저 검사 13개가 통과했다. 복제 QA 서버/Worker는 정상 종료했고 기본 앱 기존 DB 행·PID는 유지됐다. 개발 소스 `0.16.1.dev15`의 기능이며 이번 작업에서 기본 설치본은 변경하지 않았다. [검증 요약](../reports/camera_platform/condition-space-v0161/evidence-summary.json)

## 다음 소스 추가: PWM·LFM 후보 공동 사전검사

`POST /api/v1/lfm/candidate-plans`는 완료된 전기 AE Worker run에 고정된 Assembly·모델·Capture Program·Control을 읽고, 후보별 **모델 노출·gain 범위, 공동 Composer 조건, 실제 PWM 노출 격자, 선형 readout gain 구현 범위**를 함께 검사한다. Capture Program 자산의 저장 해시도 확인한다. 결과는 `blocked`, `unknown`, `eligible_for_new_frozen_run_preflight`로 구분한다. API와 Builder는 읽기 전용이며 후보용 Job이나 모델 개정판을 자동 생성하지 않는다.

[원래 8 ms PWM 사례의 후보 검사](../reports/camera_platform/lfm-candidate-plan-v0161/original-pwm-plan.json)에서 4 ms·gain 1.0은 **모델 범위에는 있지만 PWM 정책 밖**이고, 8 ms·gain 0.5는 **정책 안이지만 Lens·Sensor·ISP 모델 범위 밖**이다. 8 ms·gain 1.0은 새 Control/Test와 전체 Worker preflight를 시작할 수 있는 후보일 뿐, 200–300 DN 목표 달성이나 LFM의 물리적 성능이 입증된 것은 아니다. 모델 범위만으로 4 ms를 추천하는 오류를 막기 위해 두 계약을 한 결과에 표시한다.

관련 Python 검사 40개와 최종 데스크톱·모바일 브라우저 검사 17개가 통과했다. 분리된 QA 환경의 검증 후 서버/Worker를 종료했고, 기본 앱의 기존 DB 행·PID가 유지됨을 확인했다. 실측 자료는 0건이다. 개발 소스 `0.16.1.dev16`의 기능이며 기본 설치본은 변경하지 않았다. [검증 요약](../reports/camera_platform/lfm-candidate-plan-v0161/evidence-summary.json)

## 다음 소스 추가: gain 적용 위치를 명시한 모델 계약

개발 소스 `0.16.1.dev17`에서는 **숫자 범위만 넓힌 새 gain 후보를 자동으로 실행 준비 후보라 부르지 않는다.** 기존 `gain` 축은 아날로그 readout, 디지털, ISP gain의 의미를 구분하지 못한다. 변경 gain에 대해서는 Lens·Sensor·ISP 모델 각각에 [아날로그 gain 역할 계약](camera-platform-analog-gain-contract.md)이 있어야 하며, 선언 누락은 `unknown`이다. 전기 Capture Program의 digital gain은 고정하고 후보 gain이 실제 전기 readout의 `analog_gain`으로 전달됨을 결과에 표시한다. 원래 모델 범위와 PWM 정책에 막힌 후보는 계속 `blocked`다.

합성 0.4–1.0 모델의 역할 선언이 없는 경우 8 ms·gain 0.4는 `unknown`으로 바뀌었다. 작성자가 세 역할을 새 모델 버전에 명시하면 새 실행 사전검사 후보가 된다. 이는 작성 범위의 확인이며 gain transfer의 물리 검증이 아니다. 관련 Python 검사 42개와 모델 Factory 회귀 검사 108개, 최종 격리 브라우저 검사 18개가 통과했다. 당시 읽기 전용 UI QA는 Worker 없이 정상 종료했고 기본 DB 행·PID는 유지됐다. 당시 별도 API/Worker QA에서 Worker 종료 코드 120이 관찰되었고, 아래 dev18에서 세션 분리 후 같은 종료 경로는 코드 0으로 확인했다. [검증 근거](../reports/camera_platform/analog-gain-contract-v0161/evidence-summary.json)

## 다음 소스 추가: 실행 제출에서도 아날로그 gain 계약 강제

개발 소스 `0.16.1.dev18`은 계획과 실제 실행 사이의 간격을 닫는다. 전기 AE run의 프레임 gain이 Assembly 기준값에서 바뀌면, 제출과 Worker 재검사에서 해당 Lens·Sensor·ISP의 아날로그 gain 역할 선언을 요구한다. 숫자 범위만 넓힌 합성 모델은 `unknown` 계획에 그치고 실제 Job 제출도 거부된다. 역할을 명시한 합성 개정판의 RAW 실행은 통과했다. 기존 gain 그대로인 원래 PWM run은 유지된다.

API가 Worker를 별도 프로세스 세션으로 시작하도록 바꿔, 격리 QA 터미널의 Ctrl-C가 Worker에 중복 전달되지 않도록 했다. 최종 격리 QA에서 API HTTP 200과 후보 검사 HTTP 200을 확인하고, 종료 시 Worker 코드 0·포트 닫힘·기본 DB 행/PID 불변을 확인했다. 이 검증은 실측 gain transfer나 물리 LFM 성능을 증명하지 않는다. 추가 회귀 검사 중 임시 시험 디렉터리의 디스크 공간 부족으로 SQLite 오류가 발생했으며, 이번 작업의 임시 파일만 정리한 후 해당 캡처 분석 검사 14개는 통과했다. 호스트 디스크 여유가 약 480 MiB라 대규모 출시 검증 환경으로는 부적합하다. [검증 근거](../reports/camera_platform/analog-gain-submission-v0161/evidence-summary.json)

## 다음 소스 추가: 일반 모델의 미소유 조건 축 거부

개발 소스 `0.16.1.dev19`는 Assembly에 추가된 숫자 작동 조건이 어느 pinned 모델의 `valid_domain`에도 없는데 Composer가 `PASS`를 반환하던 간극을 막는다. 이제 그 축은 `unclaimed_condition_axis`로 `INCONCLUSIVE`다. 기존 Scene Camera는 렌즈의 고정 `properties.f_number`를 별도 정확값 계약으로 사용하므로 이를 명시적 소유자로 인정하고, 다른 값이면 `lens_f_number_mismatch`로 거부한다. 광학·픽셀·Factory·Scene Camera 검사 157개와 전기 제어·LFM 검사 46개가 통과했다. 이 검사는 선언 누락을 찾는 preflight이며, 렌즈·픽셀·전기 모델이 둘 이상의 물리 조건에서 정확하다는 증거는 아니다. [검증 근거](../reports/camera_platform/condition-owner-v0161/evidence-summary.json)

## 정적 물리 경로: 수신 ROI halo와 기본 ISP 연결

개발 소스 `0.16.1.dev20`은 이미 저장된 절대 광입력의 광학 PSF 계산에서 **source support와 수신 crop을 분리**한다. `scene.sampling.capture_crop`이 지정되면 센서 내부에서 필요한 PSF halo를 입력이 모두 제공해야 하며, 없으면 preflight가 거부한다. 원래 공급된 바깥 광량을 합성한 후 native 좌표의 수신 ROI에 RI·픽셀 응답·readout·ISP를 적용한다. 등록 장면의 `spatial_scene_v1` Job도 `parameters.capture_crop`을 제출/Worker에서 보존한다. 기존 요청의 어두운 외부 경계 계약은 유지한다.

공통 `response_chain.evaluate_response`에는 명시적 `isp_profile` 처리 경로를 추가했다. 전기 취득의 원본 정수 `raw_dn`을 기본 software ISP에 전달하며, RAW와 처리 중간값·RGB를 별도로 반환한다. 프로필 변경만의 실제 재처리는 저장 RAW를 `isp.process_raw`에 넣는다. 독립 정적 ledger 시험은 작성한 분광 광휘→절대 광량→비대칭 PSF→R/Gr/Gb/B 전하→정수 RAW→기본 ISP 한 픽셀을 대조하고, 2/5/9 파장 격자의 광전하율이 해석식으로 수렴함을 확인한다. 이것은 계산 경로 증거이며, 실물 렌즈/픽셀 정확도 또는 release 승격이 아니다. 전하 재분배와 halo ROI의 동시 사용, 전체 frame과 crop 사이 ISP 경계 일치, 동적 PWM/HDR은 남은 작업이다. [정적 경로 문서](camera-platform-spatial.md) · [검증 근거](../reports/camera_platform/static-physical-path-v0161/evidence-summary.json)
