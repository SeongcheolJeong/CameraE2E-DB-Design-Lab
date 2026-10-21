# RAW 기반 전역 평행이동 HDR

`reference_hdr_v1`에 opt-in `camera_reference_hdr_merge_global_translation_v1`
프로파일을 추가했다. 기존 `camera_reference_hdr_merge_v1`과 비선형 v2의 결과는
그대로 유지한다. 이 프로파일은 서로 독립적으로 reset·readout한 **global shutter**
브래킷에서 일정 광원과 동일 native RGGB geometry를 요구한다. 입력은 실제 정수 RAW,
각 노출 시간, black/white/full-well/dark/read/quantization 보정과 crop 기준뿐이다.
장면 이미지, 광전자 정답, 움직임 벡터 또는 시뮬레이터의 clipping truth를 합성에
전달하지 않는다. PWM, rolling shutter, 비선형 readout 프로파일은 합성 전에 거절한다.

Workbench **기준 노출 보존 합성 프로그램**에서 **새 RAW 전역 평행이동 HDR 프로그램**
버튼을 누르거나 API의 capture-program 템플릿에
`mode=reference_guarded_translation`을 지정한다. 연결하는 적용 설정의 shutter는
`global`, `line_time_ns`는 0이어야 한다. 템플릿은 기준 프레임 index 0과 다음
명시적 등록 조건으로 시작한다.

등록된 3D 렌더의 광학 halo를 보존한 수신 ROI도 사용할 수 있다. 이때 같은
`capture_crop`을 Test 초안과 `submit_reference_hdr(..., capture_crop=...)`에
명시하고, 이동 검색 반경을 제외한 내부 픽셀 수가
`minimum_common_pixels` 이상이어야 한다. 예를 들어 12×12 ROI에는
`max_shift_px=2`, `minimum_common_pixels=32`를 지정할 수 있다. 실제 공통
유효 RAW가 부족하면 비기준 프레임은 합성에 참여하지 않는다. Test와 결과의
`source_support`·`sample_geometry`는 원본 렌더 범위와 수신 ROI를 구분해 보존한다.

```json
{
  "schema": "camera_reference_hdr_merge_global_translation_v1",
  "reference_frame_index": 0,
  "base_profile": "complete camera_hdr_merge_v1 object",
  "registration": {
    "max_shift_px": 4,
    "minimum_common_pixels": 64,
    "maximum_mean_clipped_chi2": 4.0,
    "minimum_score_gap": 3.0
  }
}
```

각 비기준 프레임에 대해 기준 RAW 좌표 `(y,x)`와 후보 RAW의
`(y+dy,x+dx)`를 비교한다. `dy,dx`는 `[-max_shift_px,+max_shift_px]` 범위의
짝수 정수이며 기본 한도는 ±4 픽셀이다. 짝수 이동만 허용하므로 CFA 색 위치가
같고 보간이 필요하지 않다. 모든 후보 이동에서 동시에 유효한 동일한 내부
픽셀 집합을 사용해
`mean(min((rate_ref-rate_shifted)^2/(variance_ref+variance_shifted),25))`
를 계산한다. 내부 RAW 전자율과 분산은 기존 보정·censoring 함수에서 얻는다.
동일한 지지 집합은 가장자리와 clipping 차이가 특정 이동에 유리한 점수를 주는
것을 막는다. 최소 유효 픽셀 수, 최저 점수와 2위와의 차이 모두 명시된 임계치를
통과해야 0이 아닌 이동을 받아들인다. 0 이동도 절대 점수 한도를 통과해야 한다.
이 조건을 만족하지 못한 프레임은 전체 출력에서 기여하지 않는다.

받아들인 프레임만 기준 좌표로 정수 이동한 뒤, 기존 noise-scaled 쌍별 RAW 전자율
검사를 다시 수행한다. 불일치 픽셀의 reflected 3×3 이웃은 기준 프레임만 사용하고,
일치 영역은 선택된 유효 RAW를 역분산 가중 합성한다. 후보 이미지의 바깥 영역은
유효하지 않으며 기준 노출 하나만 남긴다. 기준 RAW가 floor, full-well 또는 ADC
상한 때문에 무효라면 다른 프레임으로 채우지 않는다. 출력 전자율·추정 분산은
NaN, 가중치는 0, validity는 false다. 원본 `raw_frames`와 프레임별 ISP 출력은
바뀌지 않는다.

새 결과에는 `hdr_aligned_exposure_rate`, `hdr_aligned_exposure_estimated_variance`,
`hdr_aligned_exposure_valid_mask`, `hdr_registration_contribution_mask`가 추가된다.
기존 `hdr_exposure_rate`는 취득 RAW 좌표의 값을 그대로 보존한다. 가중치는 **정합된**
전자율에 적용한다. `summary.hdr_merge.registration.shifts`는 각 프레임의 상태,
`[dy,dx]`와 비교 점수·공통 유효 픽셀 수를 기록한다. `translated`만 비영 이동을
허용하며 `rejected_ambiguous` 또는 유효 내부 부족이면 해당 프레임을 사용하지
않는다. Workbench 결과의 합성/기준 fallback 숫자와 상세 summary에서 확인할 수
있다. 자원 검사는 후보 비교 작업 `24,000,000` 프레임·픽셀·이동 단위 한도와
배열 메모리 예산을 캡처 전에 검사한다.

이는 화면 전체의 하나의 정수 평행이동을 RAW에서 **추정**하는 제한적 방법이다.
국소 움직임, 가림, 다른 깊이, rolling distortion, subpixel 위치, 노출 중 blur와
PWM의 물체·광원 상관은 정합하지 못한다. 노이즈나 주기 무늬가 우연히 낮은 점수를
만들 수 있고, RAW 일치가 실제 대응점이라는 증명은 아니다. 따라서 완전한
deghosting 또는 HDR/LFM 성능 보증으로 해석하면 안 된다. 출력 분산도 채택한
정합과 가중치에 조건부인 plug-in shot/read/quantization 추정이며 정합 실패,
선택 편향, 실제 보정 오차와 물리적 total uncertainty는 포함하지 않는다.
측정 기반 검증은 아직 없다.

`test_reference_hdr_translation.py`는 독립 scalar 분산·가중치와 정수 이동,
가림 fallback, 가장자리, 모호한 정합 거절, 기준 clipping을 확인한다.
`test_reference_hdr_runtime.py`는 기존 RAW/ISP bytes 보존과 rolling/작업량 사전
거절을, `test_reference_hdr_workflow.py`는 등록 Test→Worker→RAW/정합 배열→bundle을
확인한다. `browser/reference-hdr-ui.cjs`는 템플릿 노출과 form 거절을 확인한다.
