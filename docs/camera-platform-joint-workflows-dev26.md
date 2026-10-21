# Camera Platform dev26: PSF 적합성, LFM 시간 적분, 등록 RAW ISP 탐색

이 문서는 dev26 시점 기록이다. Object 거리와 RAW 처리 조건 검사를 보강한 현재 개발판은 [dev27 문서](camera-platform-joint-workflows-dev27.md)를 따른다.

현재 버전은 `0.16.2.dev26` 개발판이다. [기존 Lens×Sensor 작업실](camera-platform-integrated-workflows.md)은 저장된 **기하 raytrace PSF convolution**을 실제로 사용한다. 이 사실은 회절 포함 PSF, 렌즈 절대 광량·CRA와 센서 입사각 응답의 결합, 또는 제품 수준 MTF 정확도를 입증하지 않는다. 아래 세 작업은 그 경계를 유지하면서 계산 경로와 검증 근거를 추가한다.

## 저장 Lens DB 처방의 회절 경로

`p0014:intermediate` Lens revision `6c48e967-74b4-5901-8186-ba6fafe4797b`의 검토 CSV와 SQLite prescription을 해시·처방 fingerprint로 묶고, [US10310255B2의 Numerical Embodiment 1 원문](https://patents.google.com/patent/US10310255B2/en)을 별도 보관 HTML과 대조했다. 원문 표의 `Effective diameter` 열은 **직경**이다. 번호를 가진 28번째 행은 `∞`, 영상면까지 0.50 mm, 직경 50.00 mm이고 그 뒤에 별도의 `Image plane` 표기가 있다. 따라서 [dev25 기록](camera-platform-joint-workflows-dev25.md)의 “반경·직경 규약과 28번째 행 역할이 미확인”은 이 정확한 원문에 대해서는 수정한다. 다만 표의 유효 직경을 실제 hard clear aperture로 취급하는 것은 계속 연구용 proxy이며, nd/Vd 기반 유리 분산과 절대 투과율도 확인되지 않았다. 이 해석은 해당 revision·CSV 해시에만 결합된다.

기존 Factory의 80° 상한은 후면 **전체 방향 광량 quadrature**에 대한 것이다. 이를 OPD pupil ray 추적의 독립 수치 지원 한계와 분리했다. 원래의 방향 광량 모델은 여전히 보수적 상한 88.8702°로 차단하지만, OPD 요청은 동일한 28개 면을 유지한 채 RayOptics/Prysm으로 실제 실행된다. [재현 증거](../reports/camera_platform/integrated-workflows-20260928/p0014-patent-opd-dev26-proof.json)에서 기본 16/32 pupil refinement의 PSF kernel L1 변화는 `0.6918`(허용 `0.03`), 최소 보존 에너지는 `0.1138`(요구 `0.95`), 이웃 OPD 차이는 최대 `4.5184` waves다. 결과는 **`REFUSED`이며 승인된 회절 PSF model candidate는 없다.** 이는 단순한 “입력 지원 불가”를 넘어 수치 수렴 실패를 실제로 드러낸 진전이다. 더 촘촘한 입력으로 자동 승인하거나 저장 기하 PSF를 회절 PSF로 바꾸지 않는다.

재현 명령은 `tools/verify_camera_patent_wavefront_dev26.py`에 있다. 검토 CSV와 DB revision, 보관 특허 HTML의 정확한 바이트가 필요하다. 해당 HTML은 별도 `Lens_Patent_DB` 자산이므로 wheel에 포함하지 않는다. 제품 PSF에는 물리 개구·재료/코팅·초점 조건, pupil/field/파장 수렴, 절대 광량, CRA×native pixel angular response가 추가로 필요하다.

## 결합 LFM 시간 적분 교차 검사

`lfm_temporal_parity.crosscheck_pwm_temporal_capture()`는 고정된 Lens×Sensor 응답, 한 PWM 위상·한 노출에 대해 결합 LFM의 해석적 행별 전하 평균을 별도 정수 ns overlap과 기존 `pyisetcam.sensor_compute_temporal()` 적분으로 비교한다. [실행 증거](../reports/camera_platform/lfm-temporal-parity-20260928/dev20-pinned-phase-parity.json)는 결합식과 정수 시간 기준의 최대 상대 차이 `4.31e-10`, 시간 엔진 128표본의 `0.0035575`, 512표본의 `0.0006880`을 기록한다. 수치 분해능을 높일 때 오차가 감소했다. [명령·음수 FFT ringing 처리 경계](camera-platform-lfm-temporal-parity.md)에 자세히 기록했다.

이 검사는 기존 저장 LFM NPZ를 다시 대조한 것이 아니며, 한 작성 위상·정적 장면·가상 8×8 창에 국한된다. 제품 sensor die crop·row-zero, line time, 실제 LED와 HDR 촬영 순서는 미확인이다. 기존 HDR 결과는 양쪽 bracket fusion `8/256`, 단일 fallback `105/256`, 무효 `143/256`으로 **`inconclusive`**를 유지한다.

## 원본 RAW 기반 ISP 목적·제약 탐색

`source_bound_isp_optimization`은 등록된 비선형 RAW 네 건을 tuning flat/texture와 출처가 분리된 holdout flat/texture로 결합한다. 기존 `camera_isp_study_v2` Worker를 통해 최대 12개의 denoise/sharpen 후보를 계산하고, 독립 reference 대비 질감 진폭·편향 및 잡음 상한을 만족하는 후보 중 반복 프레임 pre-tone 잔차가 가장 작은 하나를 고른다. 선택한 후보와 기준만 별도 holdout에서 재평가하고 Study·선택 기록의 해시를 receipt에 고정한다. 원본 RAW는 보존된다. [입력 계약과 실행 예](camera-platform-source-bound-isp-optimization.md)에 작업 예산·출처 조건을 적었다.

이것은 **등록된 RAW와 선언된 지표에 한정된 탐색**이다. 독립 reference의 정확도나 제조사 ISP 동등성이 검증된 것은 아니다. 기존 FACA 최적화는 scene 재실행과 다른 ISP 계약에 기반하므로 이 RAW 지표로 잘못 승격하지 않았다. 범용 고급 ISP, HDR 동영상 정합, 화질·인지 다목적 최적화는 남아 있다.

## 제품 판단 경계와 다음 검증

공통 각도 격자 MTF는 native ROI 면적 불일치를 고쳤지만 유한 픽셀 적분과 파장별 유효 면적은 맞추지 못했다. 작은 합성 장면의 mAP `0.667`은 독립 정답이 없는 연결 진단이다. 두 수치로 렌즈·센서 제품을 순위화하지 않는다. [dev20 평가 기준](camera-platform-joint-workflows-dev20.md)을 유지한다.

다음 우선순위는 (1) 같은 처방·초점·개구·파장·field에서 수렴하는 OPD/PSF와 광량 및 센서 방향 응답, (2) MTF 픽셀 적분 면적과 독립 reference, (3) 원본 HDR/LFM artifact·다중 위상·제품 mode timing 검증, (4) 실제 RAW·라벨·과제 지표에 근거한 ISP/인지 목적함수다. 이 관문 전까지 `physical_validation=not_established`, `product_ranking_allowed=false`다.
