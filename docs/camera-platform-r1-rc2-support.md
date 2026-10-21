# CameraE2E R1 0.16.1rc2 지원 범위

이 문서는 **0.16.1rc2 엔지니어링 설치 후보**의 범위를 고정한다. 제품 승인과 실물
카메라 예측 정확도는 확립되지 않았다. 모든 시뮬레이션 결과의
`physical_validation=not_established`, 불확실도 `unknown`을 유지한다. 설치본
검증과 결합 상태는 [RC2 증거](../reports/camera_platform/r1-rc2-20260927/installed-proof-release/install-evidence.json)와
[RC2 manifest](../reports/camera_platform/r1-rc2-20260927/product-release-manifest.json)에서
판정한다. 기존 [RC1 기록](../reports/camera_platform/r1-rc-20260927/product-release-manifest-rc3.json)은
그대로 보존한다.

**R1 RC의 정식 지원 선언 대상은 등록된 16×16·500, 600 nm 설계 실험 경로 하나다.**
모델·장면·노출·gain 선택, 정수 RAW와 고정 ISP RGB, 최대 36후보·최대 128 captures의
유한 비교, 선택 저장·재열기가 이 경로에 속한다. 저장 RAW ISP 재처리는 새 RC2에서
취득한 사례에만 연결한다. 아래 선택형 시간·HDR·DCG는 **단일 등록 예제의 제한된
설치 파일럿**이다. 기본 공간·시간/HDR/DCG의 임의 조합은 후속 검증 대상이며,
이 문서의 한정된 범위를 넘어 자동 승격되지 않는다.

| 기능 | RC2에서 실행 가능한 범위 | 경계 |
| --- | --- | --- |
| 정적 설계 실험 | 패키지에 포함한 광학·픽셀·전기 조합 모델 6개와 장면 2개. Lens 광량·위치별 PSF/상대조도·입사각 → QE/전하·잡음·gain·포화·ADC → 정수 RAW → Float64 ISP RGB → 후보 평가·저장. 부품 ID·revision·hash를 보존한다. | 등록된 조합만 선택한다. 광학·픽셀·전기 부품을 임의로 자유 결합하거나 실물 성능을 예측한다고 주장하지 않는다. 16×16, 정확히 500/600 nm 2점 축, 최대 36후보·최대 128 captures. |
| 저장 RAW ISP 재처리 | 한 완료 사례의 **원본 정수 RAW**를 그대로 읽어 WB 3채널 gain, 3×3 CCM, linear/sRGB tone을 명시적으로 바꾸고 별도 RGB NPY/PNG·수치 차이·프로필·출처 해시를 저장·재열기·내보낸다. 새 광학/전기 취득은 0회다. | RC1 사례의 RAW 형식/좌표 정보가 불완전하면 **조회만 가능**하고 재처리는 거부한다. bilinear demosaic, black level, Software ISP v1에 한정한다. 색 정확도와 HW ISP 등가성은 미검증이다. |
| 선택형 시간 촬영 파일럿 | `seed-r1-modes`가 설치 패키지 예제를 Registry에 명시적으로 등록한다. 단일 clear 카메라, 16×16·500/600 nm, 작성된 두 이상 영상의 17-knot 변화, 실제 적용 설정 4프레임, 정수 RAW와 RGB를 기존 작업실에서 선택·저장·재열기한다. | authored 이상 영상 보간이며 일반 3D 움직임·가림이나 실측 시간 응답이 아니다. 등록 Assembly/Scene/Control/Test와 4–8 ms, 25°C, gain 1만 확인했다. |
| 선택형 정적 HDR 파일럿 | 같은 정적 영상에서 **4개 독립 reset 노출**을 `hdr_bracket_v1`과 고정 Capture Program으로 취득한다. 4×16×16 원본 정수 RAW, RGB와 병합 결과를 별도로 보존한다. | 장면 움직임 정렬, PWM/LFM, 잔류 전하와 결합한 HDR은 범위 밖이다. |
| 선택형 공유 전하 DCG 파일럿 | `shared_charge_dcg_v1`은 취득마다 **한 전하의 HCG/LCG 두 readout**을 계산한다. RAW 축은 `[acquisition, readout, row, column]`인 4×2×16×16이고, 정수 원본·RGB·병합 출력을 보존한다. | 독립 HDR 브래킷과 같은 취득으로 비교하지 않는다. 임의 센서/회로 및 vendor DCG 정확도는 미검증이다. |
| 실험·조회 | 기존 이동·PWM·LFM 진단, source-position RI, 고급 ISP, 최적화·공차/Agent 연구는 별도 개발 코드와 증거에서 조회한다. | RC2 설치본의 일반 조합, 성능 또는 실물 정확도 보증으로 사용하지 않는다. 실제 시간 신호를 바꿀 수 있는 RAW 필터는 기본 비활성이다. |

시간/HDR/DCG 파일럿은 RC1의 별도 5파장 demo 자산이 아니라 **R1 패키지의
2파장 예제**에서 생성한다. `seed-r1-modes`는 R1 project marker와 정확한
패키지 자산을 요구하며, 외부 자산 override를 허용하지 않고 재실행 시 같은 ID를
반환한다. 분광 radiance와 on-axis Lens E/L은 저장된 이상 영상에 **한 번만** 적용한다.
Registry Lens는 그 광량을 다시 적용하지 않고 패키지의 PSF·상대조도·입사각을
사용한다. [브리지 시험](../tests/platform/test_r1_registry_seed.py)은 동일 조건의
정적 정수 RAW가 기존 R1 계산과 정확히 일치하는지 확인한다. 이 구조는 한 등록
Assembly의 수치 연결을 증명하며 일반 Lens/Pixel/Electrical 모델 조합의 보편성을
증명하지 않는다.

설치 검증은 패키지 외부의 새 프로젝트와 새 venv에서 API·Worker·브라우저로
실행하고, R1 상태를 백업·복원해 원본 RAW/재처리/모드 실행을 다시 읽는다. 검증기
`camera_platform_r1_install_evidence_v1`의 기존 12개 관문에
`design_isp_replay`, `selected_temporal_hdr_dcg`를 추가한다. 두 관문은 RAW
불변·재취득 0회·RGB 차이, 등록 입력·광량 중복 방지·세 모드의 실제 artifact,
HDR UI 제출/재열기, 복원 후 동일성을 요구한다. 정적 설계 실험의 공개 최대 입력
123회 실제 취득에 대한 시간·sampled peak RSS·저장량·목록 조회 비용도 별도
기록한다. 그 측정값이 시간/HDR/DCG의 일반 최대 성능을 뜻하지 않는다.

[R1 release 생성기](../tools/create_camera_platform_r1_release.py)의 `rc_ready`는
정확한 wheel·지원 문서·설치 증거를 해시로 결합한 **검토 가능 상태**이다.
`approval_status=unapproved`를 유지한다. 기존 실측 Release gate를 약화하지 않는다.
기본 v0.15 앱·환경·데이터는 유지하고 R1을 별도 state에 설치한다. 시작 명령과
백업/복원 절차는 [RC2 빠른 시작](camera-platform-r1-rc2-quickstart.md)에 있다.
