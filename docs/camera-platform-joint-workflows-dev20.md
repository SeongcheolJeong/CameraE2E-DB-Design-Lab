# Camera Platform dev20: 동일 물체각 격자와 제품 근거 분리

이 개발판은 [dev19 설치본에서 드러난 균일 격자 한계](camera-platform-joint-workflows-dev19.md)를 해결하고 [dev18 결합 RAW·시간·인지 실행](camera-platform-joint-workflows-dev18.md)을 잇는다. Lens DB × Sensor DB RAW는 여전히 `pyisetcam`의 저장 raytrace PSF convolution을 사용한다. 이번 작업은 PSF를 사용한다는 사실과, 그 PSF로 제품 결론을 낼 수 있다는 주장을 분리한다.

저장된 실행 가능 Lens v2 PSF 자산 4건은 모두 **기하 raytrace PSF**다. 생성기와 manifest에는 `diffraction_included=false`, `radiometry_status=unknown`, 상대조도와 분광 투과율의 unity placeholder, 미검증 pupil·분광 sampling이 기록돼 있다. chief-ray angle과 pupil 방향은 기하 진단으로 존재하지만, 선택한 `db_estimate_v1` 센서 run에는 입사 방향별 픽셀 응답이 없다. 따라서 회절 blur, 렌즈 광량, CRA 보정을 임의로 더하지 않는다. 그 값을 제품 비교에 사용하려면 같은 처방·초점·개구·파장·field의 회절 포함 PSF 또는 OPD와 sampling 검증, 보정된 투과율·상대조도, 센서의 파장×입사 방향 응답이 필요하다.

화질 Job의 v2 계약 `camerae2e_lens_sensor_quality_study_v2`는 저장 2×2 RAW 네 장을 같은 ISP로 처리한 뒤 두 가지 진단을 구분한다. 기존 `matched_object_angle_edge_mtf50`은 공유 각도 겹침 안의 **서로 다른 native ROI 면적**에서 계산했으므로 과거 기록에 그대로 남긴다. 새 `common_grid_object_angle_edge_mtf50`은 각 카메라의 검증된 550 nm 기하 픽셀 중심 지도와 유효 ISP 영역 안에서, 네 영상 모두를 동일한 중앙 물체각 표본 격자에 안티앨리어싱 후 보간해 ISO edge MTF50을 다시 계산한다. 공통 균일 격자에 16×16 이상을 담을 수 없으면 가장 거친 센서의 검증된 native 중심 격자를 사용하고, 다른 세 영상을 그 **정확히 같은 좌표**에만 보간한다. 경계 필터는 보존된 전체 ISP 프레임을 반사 연장하며 이 근사도 결과에 표시한다. 각 입력 장면·노출·CFA·PSF 조건, 원본 RAW, 격자 해상도·Nyquist·색별 중심 차이의 기존 제한도 유지한다. 어느 방식으로도 공통 16×16 격자를 만들 수 없거나 입력이 포화·무효면 숫자를 내지 않는다.

이 ‘면적 일치’는 **550 nm 기하 픽셀 중심 좌표의 면적**에 한정된다. 유한 픽셀 적분과 파장별 PSF의 유효 각도 footprint는 아직 동일하게 검증되지 않았다. 새 지표 역시 ISP 후단의 합성 edge 민감도이지 실측 시스템 MTF가 아니다. 결과의 `pixel_integrated_effective_area_matched=false`, `product_ranking_allowed=false`, `physical_validation=not_established`를 유지한다. 저장된 결과를 다시 열 때에는 봉인한 RAW·ISP 배열에서 두 진단을 재계산해 변조를 거부한다. v1 요청·기록의 읽기는 유지한다.

인지 평가에는 `synthetic_contract_fixture` 주석 출처를 추가했다. 기존 dev18 설치 사례의 작은 `uniform`·`slanted_edge` 장면에 작성한 박스는 실제 물체의 독립 정답이 아니다. 새 출처는 `ground_truth_independence=semantic_ground_truth_absent_synthetic_contract_fixture`를 명시하며, 두 이미지의 mAP는 API·SDK·Worker 연결 진단으로만 남긴다. 기존 `human_annotation`과 `independent_reference` 저장 결과의 문자열·해시 계약은 유지한다. 대표 장면과 독립 라벨, 실제 모델 추론이 오기 전까지 `product_map.available=false`다.

HDR/LFM의 `completed`는 계산기가 출력 배열을 만들었다는 뜻일 뿐 제품 성능 판정이 아니다. [dev18 설치 사례](../reports/camera_platform/integrated-workflows-20260928/joint-dev18-installed-timed-proof.json)에서는 2노출×4위상×8×8 중 양 브래킷 fusion이 8픽셀, 단일 브래킷 fallback이 105픽셀, 무효가 143픽셀이어서 최종 판단이 `inconclusive`다. 행 시간 JSON의 SHA는 입력 파일의 정체성만 증명한다. 선택한 저장 센서 readout은 `generic_assumption`·`unmeasured_assumption`이며, 제품 line time·HDR mode·LFM schedule과 crop의 실제 die 위치는 확인되지 않았다. 이 누락을 숨긴 채 재구성 상태를 제품 결론으로 승격하지 않는다.

`hdr_lfm_evidence_readiness.py`는 완료한 HDR 보고서의 픽셀 수 합계·coverage fraction·원본 RAW와 source LFM 해시를 다시 확인한다. `model_internal_coverage`, `vendor_mode_simulation`, `product_hdr_lfm_decision`을 별도 상태와 blocker 목록으로 출력한다. `python -m camera_platform.hdr_lfm_evidence_readiness <HDR_REPORT_JSON> --sha256 <REPORT_SHA256> --npz <HDR_EVIDENCE_NPZ> --source-lfm-result <LFM_RESULT_JSON> --output <NEW_READINESS_JSON>`로 기존 저장 파일을 변경하지 않고 실행할 수 있다. 동반 파일을 주지 않으면 재확인되지 않은 출처를 blocker로 남긴다. 선언된 vendor 문서가 있더라도 line time의 독립 검증과 실제 mode capture program이 없으면 제품 판정은 계속 `not_established`다.

기존 CameraE2E optimizer는 실제 저장 렌즈 PSF·센서 snapshot으로 노출 후보를 실행할 수 있음을 확인했다. 다만 플랫폼에는 노출·gain grid가 이미 있고, 그 기능을 하나 더 등록해도 광학/각도 MTF, HDR 강건성, 인지 지표의 공통 목적·제약을 해결하지 않는다. 다음 최적화 연결은 해당 지표의 유효성·출처가 준비된 변수부터 허용해야 한다. 렌즈 f-number, 픽셀 pitch, CFA를 저장 PSF·센서 revision을 고정한 채 독립 변수로 바꾸면 모델 정체성이 깨진다.

## 설치본 검증 범위

[dev20 wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev20-py3-none-any.whl)의 SHA-256은 `528923783f68359d8081f410e47dd054ce9bac6a217587036e13f10e2b029b5e`다. 별도 설치 경로의 1,033개 파일이 wheel과 바이트 일치함을 확인했고, 세 경로를 각각 새 Registry와 child Worker에서 실행했다. 기존 8770 서버나 일반 설치 위치를 교체했다는 증거는 아니다.

| 설치본 경로 | 결과 | 증거 |
| --- | --- | --- |
| Lens DB 2개 × Sensor DB 2개 저장 RAW → 같은 ISP → v2 MTF | 동일 256×256 D65 edge 입력과 네 32×32 정수 RAW를 사용했다. 가장 거친 L2S2의 검증된 16×16 기하 중심 격자에 네 영상을 맞춘 MTF50은 L1S1 55.444, L1S2 55.378, L2S1 75.700, L2S2 76.252 cycles/degree다. `product_ranking_allowed=false`다. | [화질 Job 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev20-quality-installed-proof.json) |
| 결합 RAW → ISP → 제공 예측 → 인지 코호트 | 서로 다른 두 합성 장면·이미지 ID와 `synthetic_contract_fixture` 출처를 보존했다. 진단 mAP는 0.667이며 `product_map.available=false`다. | [인지 Job 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev20-perception-installed-proof.json) |
| 출처 고정 PWM/LFM RAW → HDR → 근거 판정 | 256개 위상 픽셀 중 양 브래킷 fusion 8, 단일 브래킷 fallback 105, 무효 143이다. RAW와 source LFM 해시를 재확인한 뒤 모델 계산 상태는 `inconclusive`, 제품 HDR/LFM 판단은 `not_established`다. | [HDR 근거 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev20-hdr-readiness-installed-proof.json) |

최종 소스의 관련 화질·인지·HDR 계약 테스트 58개, workbench JavaScript 계약, 변경 파일 Ruff와 `git diff --check`가 통과했다. 이 문서는 물리 정확도나 상용 제품 준비가 완료됐다는 주장이 아니다.
