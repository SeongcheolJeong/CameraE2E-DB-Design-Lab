# CameraE2E R1 지원 범위 동결안

**상태: 0.16.1rc1 엔지니어링 RC 설치 검증 통과, 제품 승인 전.** `R1 지원`은
아래의 정적 설계 실험 한 경로에만 적용된다. `후보 지원`은 등록 서비스와 계산
코드에서 연결한 후속 파일럿이며 이 RC 설치본의 지원 선언이 아니다. [최종 설치본
검증](../reports/camera_platform/r1-rc-20260927/installed-proof-rc3/install-evidence.json)은
독립 자산·API·Worker·브라우저·복구와 공개 최대 입력을 확인한다. R1은
설계·시뮬레이션 엔지니어링 버전이며 실물 카메라 예측 정확도는 확립되지 않았다.

**R1 RC의 정식 지원 선언 대상은 등록된 16×16·500, 600 nm 설계 실험 경로 하나로
한정한다.** 모델·장면·노출·gain 선택, 정수 RAW와 고정 ISP RGB, 유한 후보 비교,
선택 결과 저장·재열기가 이 경로의 사용자 흐름이다. 아래 기본 공간·시간/HDR/DCG 항목은
다음 승격을 위한 개별 파일럿이며, R1 정식 지원이나 자유로운 조합을 뜻하지 않는다.

## 지원 분류

| 분류 | R1에서 표시할 의미 | 현재 근거와 출시 전 남은 확인 |
| --- | --- | --- |
| **후보 지원 — 기본** | 등록된 Assembly(Lens·Sensor·ISP의 정확한 버전), 분광 이상 영상, Test를 선택해 `spatial_scene_v1`을 실행한다. 이상 영상에 명시된 on-axis Lens 광량을 입력으로 받고 Lens의 위치별 RI·입사각·파장별 강도 PSF, Sensor QE 또는 4사이트 절대 응답, 전하·잡음·ADC, 정수 RAW, 고정 ISP RGB, 비교·원본 RAW 보존 재처리·결정 저장/재열기를 한 작업실에서 사용한다. 광량을 두 번 적용하지 않도록 조립 조건·파장·기준면·포함 효과를 사전 검사한다. | [등록 공간 실행/RAW Replay 시험](../tests/platform/test_spatial_workflow.py), [결정 저장/재열기 시험](../tests/platform/test_case_reviews.py), [공간 계산 계약](../src/camera_platform/spatial.py), [ISP v1](../src/camera_platform/isp.py). 이 별도 Assembly 사용자 흐름과 64×64 성능은 R1 설치본에서 승격 검증하지 않았다. |
| **후보 지원 — 선택형 시간 촬영** | 등록된 분광 영상의 유한 시간 knot와 기록된 적용 설정·global/rolling 노출 창으로 RAW/RGB를 계산한다. 명령값과 실제 적용값을 구분한다. | [등록 실행·RAW 재처리 시험](../tests/platform/test_sequence_workflow.py), [시간 계약](../src/camera_platform/timing.py). RC 선택 화면·설치 경로 확인 후 활성화한다. |
| **후보 지원 — 선택형 정적 HDR** | `hdr_bracket_v1`의 **서로 독립 reset된** 노출 브래킷. 같은 정적 광학 영상, 정확히 일치하는 등록 Sequence/Control/Test/Capture Program을 쓰고 정수 RAW 원본과 병합 결과를 각각 보존한다. | [등록 HDR·Replay 시험](../tests/platform/test_hdr_workflow.py). 병합기는 광학 영상의 정적 동일성을 요구한다([검사](../src/camera_platform/hdr_runtime.py)); 움직임 정렬 또는 움직이는 대상의 HDR 품질은 지원하지 않는다. RC 설치 흐름에서 프로그램 작성/선택과 재열기 확인이 필요하다. |
| **후보 지원 — 선택형 정적 DCG** | `shared_charge_dcg_v1`의 **한 전하를 HCG/LCG 두 경로로 읽는** 취득. 원본은 `[acquisition, readout, row, column]`이며 독립 브래킷으로 취급하지 않는다. 명시적인 읽기 공분산과 고정 Capture Program이 필요하다. | [등록 DCG·Worker 시험](../tests/platform/test_dcg_api.py), [DCG 계산 계약](../src/camera_platform/dcg_runtime.py). 기존 단일 RAW ISP Replay는 두 경로를 묵시적으로 합치지 않고 거부한다. RC 설치 화면의 저장·재열기·병합 출력 확인이 필요하다. |
| **R1 지원 — 정적 설계 실험** | 등록된 광학·픽셀·전기 조합과 장면을 선택해 유한 후보를 실제 RAW 계산으로 비교하고, 적합 후보가 없으면 선택하지 않는다. 선택 결과를 먼저 고정한 뒤 별도 입력으로 평가하며 원본 RAW·고정 ISP RGB·선택·실패 기록을 저장하고 재열기한다. | [R1 자산 묶음](../examples/r1/manifest.json), [별도 프로젝트 모드](../src/camera_platform/project_mode.py), [최종 설치 검증](../reports/camera_platform/r1-rc-20260927/installed-proof-rc3/install-evidence.json). 등록 조합만 지원하고 실물 정확도와 자유 부품 조합은 미확립이다. |
| **실험·조회 전용** | 이동 3D/두 광원 PWM/트랩·잔류 전하, Source-position RI opt-in, LFM 진단, 고급 ISP/Prospective optimizer, 움직임·채널비 연구, 원본 시간 신호를 바꿀 수 있는 RAW 필터. | 개발 코드/수치 시험은 있지만 임의 조합이나 R1 설치 사용자 흐름은 입증되지 않았다. RAW 시간 필터는 기본 비활성으로 표시한다. |
| **R1 미지원** | 임의 혼합 깊이·연속 3D 가림/움직임, 공간·방향 완전 결합 광학, 복잡한 pixel 구조/회로·crosstalk, 움직임·PWM·잔류 상태의 자유 결합 HDR, vendor LFM/DCG/HW ISP 등가성, 전역 최적화·공차/환경 전체 탐색·적응형 Agent, 실물 정확도 보증. | 사용자 R1 범위 정의에 따라 후속 버전에서 다룬다. UI가 실행 가능 여부와 물리 검증 부재를 혼동하지 않게 표시한다. |

현재 R1 설계 실험 예제에는 **등록된 조합 모델 6개**가 있다. 기존 3개는
QE·픽셀 각도 응답·readout을 고정한 광학 구성 비교용이며, 새 3개는 같은
Lens에 픽셀 응답 또는 전기 readout 또는 둘 다를 바꾼 합성 예제다. 각 조합의
광학·픽셀·전기 component ID·revision·hash를 고정한다. 작업실은 이 등록
조합을 선택하며 임의의 부품 3개를 자유롭게 결합하는 화면은 아니다. 일반
모델의 자유 조합과 HDR/LFM·고급 ISP/최적화의 폭넓은 연결은 후속 모듈
확장 대상으로 다룬다. 실물 정확도는 여전히 검증되지 않았다.

## R1 RC 입력 봉투와 후속 파일럿 — 구현 최대치와 구분

정식 R1 대상은 **설계 실험의 등록 16×16·2파장·최대 36후보·최대 128 captures**다.
아래 기본 공간·시간/HDR/DCG 수치는 후속 승격 시 검증할 파일럿 계획이다.
엔진의 더 넓은 허용치가 자동으로 R1 지원 범위를 넓히지 않는다. 설치본은 정식
설계 실험 범위를 화면·API·문서에서 확인했다. 다른 조합의 사용자 실행·표시는
후속 파일럿 검증으로 남는다. 후속 파일럿은 등록 자산의 `valid_domain`과 정확히 일치하는 좌표를
추가로 요구한다.

| 축 | R1 정식 범위 또는 후속 파일럿 | 현재 코드의 상한·경계와 근거 |
| --- | --- | --- |
| 분광 영상 크기 | 기본 공간 경로 `64×64` native crop, 시간/HDR/DCG `16×16` receiver, R1 설계 실험 자산은 **16×16**. 두 번째 모델·장면도 해당 경로의 같은 계약에서 코드 수정 없이 실행한다. | 분광 이미지와 공간 계산은 두 변 각각 **짝수 8..512**, 2..16 파장; ROI는 Sensor 내 위치와 PSF/전하 halo가 맞아야 한다([입력](../src/camera_platform/imaging.py), [광학](../src/camera_platform/spatial.py)). `64×64` 기본과 `16×16` 시간/HDR/DCG는 위 등록 시험에서 실제 실행한 크기다. 512 한계에서의 실용 성능은 미측정이다. |
| 파장 | 기본 공간·시간/HDR/DCG 파일럿은 정확한 **450, 500, 550, 600, 650 nm** 5점 축. **별도 R1 설계 실험 예제는 500, 600 nm 2점 축**([자산 묶음](../examples/r1/manifest.json))이다. 두 계열을 같은 광학 입력으로 섞지 않는다. | 입력기는 엄격히 증가하는 **2..16점, 200..2500 nm**를 받지만 장면·Lens·PSF·Sensor의 축을 묵시적으로 재표본화하지 않는다([공간 계약](../src/camera_platform/spatial.py), [입력기](../src/camera_platform/imaging.py)). 샘플 축 이외의 연속 스펙트럼 정확도는 미보증. |
| 프레임/시간 | 기본 RAW `1..3` 프레임 중 RC 대표는 **3**, 시간 촬영은 **4 프레임·17 knot**, 정적 HDR는 **4 독립 브래킷**, DCG는 **4 취득×2 readout**을 먼저 고정한다. | 공간/시간 엔진은 최대 **16 프레임**, 시간 knot **2..128**, 시간/병합은 총 **1,048,576 frame-pixels** 제한([공간 기능](../src/camera_platform/spatial.py), [시간 계약](../src/camera_platform/timing.py)). 임의 최대치·장시간 구동을 지원한다고 표시하지 않는다. |
| Sensor/RAW | `native_sensor` crop, RGGB CFA, unpacked 1-stream 정수 RAW. Lens/PSF pitch와 Sensor pitch, full sensor geometry 및 crop phase를 일치시킨다. 기본은 고정 gain·고정 ISP v1. | 등록 공간 준비는 RGGB/unpacked/1-stream 및 Sensor bit depth/black level 일치를 검사한다([준비](../src/camera_platform/imaging.py)). 기록 시간 경로는 global 또는 rolling shutter와 명시적 적용 gain/노출을 받는다([시간 계약](../src/camera_platform/timing.py)). 자동 mode 전환, HW ISP 및 임의 CFA는 R1 지원 아님. |
| HDR/DCG | 정적 대상만 선택형. HDR는 서로 다른 reset 노출·고정 Program·2개 이상 프레임, DCG는 한 전하의 HCG/LCG 2경로·고정 Program. 둘 사이의 결과를 같은 취득으로 비교하지 않는다. | HDR 병합은 정적 광학 동일성 검사 및 PWM 전체 주기 조건을 가진다([HDR Runtime](../src/camera_platform/hdr_runtime.py)). DCG는 움직임/파형을 지원하지 않는다([DCG Runtime](../src/camera_platform/dcg_runtime.py)). R1 기본 경로에서 PWM 병합은 숨기고 실험 기능으로 둔다. |
| ISP | 정수 RAW 보존, 기존 고정 `black_level → bilinear demosaic → fixed WB → CCM → tone` Float64 프로필과 원본 그대로인 RAW의 재처리. HDR 병합은 별도 미리보기이며 DCG 2스트림을 단일 RAW로 암묵 변환하지 않는다. | [ISP v1 계약](../src/camera_platform/isp.py), [원본 Replay 경계](../src/camera_platform/imaging.py). 색도 실측 정확도·HW ISP 등가성은 범위 밖. |

## R1 출시 차단 기준과 확인 결과

정식 설계 실험 범위는 [설치본 검증](../reports/camera_platform/r1-rc-20260927/installed-proof-rc3/install-evidence.json)의
12개 gate로 판정한다. 아래 기준을 통과해도 다른 후보 파일럿이나 실물 정확도가
자동으로 승격되지 않는다.

1. **자산 독립성:** 새 환경에서 개발 checkout·과거 QA 폴더·캐시 없이 필수 자산을
   설치한다. 예제에 필요한 모델/장면/Test/Program의 ID·버전·출처를 포함하고,
   두 번째 모델·장면을 코드 수정 없이 같은 경로로 실행한다. 현재 설계 실험의
   R1 설계 실험 자산은 설치본에서 이 기준과 6개 등록 조합의 component identity를
   확인했다. 별도 Assembly/Test/Program 경로는 후보 지원으로 남는다.
2. **사용자 경로:** 같은 origin 작업실에서 모델/장면/조건 선택 → 사전 검사 →
   계산 → 원본 RAW·영상·성능 비교 → 후보 선택/저장 → 재열기를 실제 최종 패키지로
   수행한다. 시간/HDR/DCG는 각자의 Test·Program을 명시적으로 선택하게 하고,
   준비되지 않은 조합을 성공처럼 표시하지 않는다.
3. **수치/실패:** 기준면·단위·좌표·시간과 광량/개구/노출 적용을 확인한다. 원본
   불변, seed 재현성, 무후보·평가 실패·취소·중단·저장 실패 시 결과/상태를
   구분한다. 계산·교차 도구·실물 측정을 별도 검증 수준으로 기록한다.
4. **설치/성능/배포:** 고정 의존성·필수 blob·지원 환경을 가진 RC를 새 위치에
   설치하고 정식 16×16 입력 봉투의 시간·sampled peak memory·결과 저장량·목록 조회 비용을
   기록했다. 테스트한 파일 해시가 배포 파일과 같아야 한다. 기본 v0.15의 코드·
   환경·데이터는 유지하고 R1 별도 데이터로 먼저 배포한다. 기본 앱 교체/이전과
   복귀는 나중에 함께 검증한다.
5. **배포 의미 충돌:** 현 [카메라 검증 Release](../src/camera_platform/service.py)는
   `physical_validation_pass`가 없으면 `independent_validation_missing`으로
   차단한다. 이 Gate를 약화해 물리 검증에 통과한 것처럼 표시해서는 안 된다.
   R1 엔지니어링 설치 후보와 배포 기록은 별도 타입/정책으로 만들고,
   `physical_validation = not_established`를 그대로 보존해야 한다.

독립 실물 측정은 R1의 **차단 조건이 아니지만** `physical_validation =
not_established`와 불확실도 `unknown`을 화면·보고서에 유지한다. 현재 개발
`0.16.1.dev32` 증거는 [소스/시험 경계](../reports/camera_platform/physical-chain-v0161-dev32/evidence-summary.json)에
있으며 설치된 R1을 뜻하지 않는다. 마지막으로 확인한 기본 앱 버전은 v0.15.0이다.

## 별도 R1 엔지니어링 RC 기록

[R1 manifest 생성기](../tools/create_camera_platform_r1_release.py)는 기존
`PlatformService.create_release()`의 실물 검증 Gate를 바꾸지 않는다. R1
`project-mode.json`(`camera_platform_project_mode_v1`, `mode=r1_engineering`,
`project_id`)이 있는 별도 state, wheel, 이 문서, 설치 검증 JSON을 받아 해시로
묶는다. 설치 증거가 누락·불일치하면 `status=blocked`와 이유를 기록한다.
모든 요구 증거가 일치할 때만 `status=rc_ready`가 되며, 이때도 항상
`approval_status=unapproved`, `physical_validation=not_established`,
`uncertainty.kind=unknown`이다. `rc_ready`는 증거 결합을 검토할 RC 상태이지
제품 승인이나 실물 정확도 판정이 아니다.

현재 RC의 결과는 [제품 RC manifest](../reports/camera_platform/r1-rc-20260927/product-release-manifest-rc3.json)에
보존한다. manifest를 새로 생성할 때는 해당 설치 검증 JSON에 기록된 `state_dir`와
`project_id`, 정확히 같은 wheel, 이 지원 문서, 새 출력 경로를
`create_camera_platform_r1_release.py`에 전달해야 한다. 검증 증거와 다른 프로젝트
상태를 넣으면 차단된다. 설치·실행 명령은 [R1 빠른 시작](camera-platform-r1-quickstart.md)에 있다.

출력 경로는 새 파일이어야 한다. 차단 manifest도 보존하며 종료 코드는 2,
`rc_ready`는 0이다. 설치 증거 JSON은
`schema=camera_platform_r1_install_evidence_v1`, `status=passed`,
`project_id`, `mode`, 절대 `state_dir`, `project_mode_sha256`, `wheel_sha256`,
`support_doc_sha256`, `physical_validation=not_established`,
`installed_payload_manifest_path`와 그 SHA를 선언한다. 설치 payload manifest는
`camera_platform_installed_package_v1`과 같은 wheel hash·파일 hash 목록이어야
한다. 각 필수 `checks` 항목은 `status=passed`, 실제 `evidence_path`와
`evidence_sha256`을 가진다. 검증기는 다음 이름을 모두 채워야 한다:
`isolated_install`, `installed_imports`, `required_assets`, `two_model_scene`,
`numerical_contract`, `deterministic_reproduction`, `complete_user_journey`,
`failure_paths`, `restart_restore`, `performance`, `support_display_consistency`,
`release_file_identity`.

세부 필드도 빈 성공 표시만으로는 통과하지 않는다. 격리 설치는 fresh environment,
system-site/checkout import 부재를, 자산은 QA/checkout 의존 부재를 명시한다.
두 모델·장면은 서로 다른 ID가 각각 2개 이상 있어야 한다. 사용자 경로는
모델/장면/조건·RAW/RGB·비교·선택 저장·재열기 5단계가 모두 통과해야 한다.
실패 처리는 잘못된 입력·무후보·평가 실패·취소·중단·저장 실패 6가지를 모두
통과해야 한다. 성능은 공개 최대 입력의 실행 시간·peak memory·저장량·목록 조회
시간과 RAW 비재계산을 기록한다. `release_file_identity`는 wheel SHA와 실제
설치 payload manifest SHA를 함께 확인한 결과여야 한다.

R1 상태 백업은 [백업 API](../src/camera_platform/backup.py)에서
`backup_state(state, archive, expected_project_id="camera-r1-pilot")`, 복원은
`restore_state(archive, new_state, expected_project_id="camera-r1-pilot")`로
검증한다. 이 호출은 프로젝트 marker가 없거나 ID가 다른 경우 실패한다. R1 marker가
있는 상태의 백업은 `format_version=2`로 marker 내용·SHA를 포함하며, 기존
marker 없는 v1 백업은 기존 형식으로 복원한다. 목적지는 존재하지 않는 새
디렉터리여야 한다. 일반 백업 호출 전에 marker를 삭제하면 그 상태는 원래부터
marker가 없던 구형 상태와 구별할 수 없으므로, **R1 복구 시험에서는 항상
`expected_project_id`를 전달**해야 한다.
