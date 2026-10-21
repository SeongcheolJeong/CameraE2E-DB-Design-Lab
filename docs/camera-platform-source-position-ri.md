# 소스 위치 상대 조도 연산자 (`source_position_ri_v1`)

등록된 `spatial_scene_v1` 또는 `temporal_scene_v1` 실행에서
`parameters.field_radiometry_mode`를
`"source_position_ri_v1"`로 지정할 수 있다. 생략하면 이전 수신 픽셀 위치 상대 조도
연산과 출력 계약이 유지된다. 이 모드는 같은 Lens의 파장별 field RI를 이상상 소스
샘플에 적용한 다음 조건부 단위합 PSF로 수신 픽셀에 전달한다. 렌더/투영으로 만들어진
소스 광휘 아티팩트는 수정하지 않는다.

개발 소스에서는 등록된 `retained_charge_v1`에도 제한된 opt-in을 제공한다. 이 경우
trapped HCG/LCG capture program, 명시적 PWM, 서로 독립된 변조·비변조 분광
시퀀스가 모두 필요하다. 각 성분의 소스 셀/서브셀 위치에서 RI를 PSF 전에 따로
적용하고, 수신 행의 실제 PWM/비변조 적분과 공유 전하·트랩 상태를 거쳐 정수 RAW를
생성한다. 두 시퀀스가 등록된 3D Light component라면 동일 Scene의 두 광원
분할인지 추가로 검증한다. Python SDK의 `submit_temporal`과
`submit_retained_charge`는 같은 값을 `field_radiometry_mode` 인자로 전달한다.
다른 HDR/DCG topology와 photo-HDR 역산에는 이 모드를 적용하지 않는다.

```json
{
  "plugin_id": "spatial_scene_v1",
  "parameters": {"field_radiometry_mode": "source_position_ri_v1"}
}
```

파장 `λ`, 소스 샘플 `s`, 수신 픽셀 `r`에 대한 이산 연산은 다음과 같다.

`E_after_PSF(r, λ) = Σ_s E_ideal(s, λ) · RI(s, λ) · K(r | s, λ)`

`E_ideal`은 on-axis Lens throughput을 이미 한 번 포함한 `W/m²/nm` 입력이고,
`K`는 수신 픽셀 개구 적분이 이미 포함된 단위합 intensity PSF다. 소스 위치는
`full_sensor_shape`와 `crop_origin`으로 정한 native 센서 좌표다. 일반 소스 셀은
셀 중심을 사용한다. 보존된 2×2/4×4/8×8/16×16 서브셀은 각 midpoint의 분수
좌표에서 RI를 평가하고, 기존 서브셀 면적 가중치 `1/grid_side²`를 적용한다.
추가 수신 개구 필터나 수신 위치 RI 곱셈은 없다. 광전자는 이 PSF 출력에 기존
수신 위치 Lens directional response와 센서 angular QE를 적용하여 얻는다.
이 순서는 [Maeda, Catrysse, Wandell의 영상계 모델](https://web.stanford.edu/~wandell/data/papers/Maeda-Optics-SPIE-EI-5678-5.pdf)
§2.2의 이상상 위치별 RI 처리 후 PSF 필터링과 같은 계열이지만, 여기서는
명시된 이산 native 격자·분리 방향 응답에 한정한다.

실행 전 입력·추가 RI 배열의 합산 float64 크기를 64 MiB 기본 Runtime 한도 안에서
검사하고, 모든 소스 셀/서브셀 중심이 Lens RI field 반경 안에 있는지 검사한다.
반경 외삽은 거부한다. 기존 PSF/FFT/서브셀 경로 예산과 capture ROI halo 검사는
그대로 적용된다. 요청 모드는 frozen run 요청과 실행 시 대조한다.

출력의 `source_relative_illumination_map`은 소스 support 형태의 무차원 배열이다.
소스 셀은 `[source_row, source_col, wavelength]`, 보존 서브셀은
`[source_row, source_col, subrow, subcol, wavelength]` 축을 갖는다.
`flux_diagnostics.pre_ri_source_spectral_flux`와
`post_ri_source_spectral_flux`는 각각 PSF **전** 소스 평면의 파장별 `W/nm`다.
`input_spectral_flux`는 이 모드에서 post-RI 입력과 같다.
`relative_illumination_map`은 하위 호환을 위해 계속 제공하지만, 이는
**수신 위치 진단 지도이며 실제 곱하지 않는다**. 결과 summary의
`relative_illumination_application`에 적용 위치와 잔여 근사를 명시한다.
시간 장면의 추가 계약, knot별 flux ledger와 행별 적분 범위는
[temporal source RI 문서](camera-platform-temporal-source-position-ri.md)에 정의한다.

독립적인 셀/서브셀 직접 합산 oracle은 소스 위치 RI → PSF → native 4-site
전자량 → 정수 RAW를 검사한다. 비중심 receiver ROI도 필수 PSF halo가 있는
source support와 full-frame 결과를 비교한다. 이 검사는 선택한 이산 연산자의
산술/전파 검증이지 측정 렌즈·센서의 정확도 검증이 아니다.
별도 oracle은 생성 효율과 인접 pixel 전하 이동을 결합해 소스 RI → 비대칭 PSF →
source CFA 생성 → collection → RAW 순서를 확인하고, 광학·전하 이동의 합성
halo가 있는 ROI의 full-frame 동등성을 검사한다.

별도의 **기존 경로 버그 수정**: 보존 서브셀 + field PSF + 비중심 capture ROI에서
PSF 평가에 receiver crop 원점이 전달되던 것을 source-support 원점으로 고쳤다.
이 수정은 mode를 선택하지 않은 기존 서브셀 경로에도 적용되며, 독립 full-frame
직접 합산 oracle/RAW 회귀로 검증한다. 일반 셀의 mode 생략 경로는 그대로다.

남은 한계: directional response와 angular QE는 수신 위치에서 독립적으로
평가하는 분리 근사이며, 소스별 광선 방향·PSF·픽셀 수집의 결합 연산은 아니다.
보존 서브셀의 분수 위치 PSF도 이산 receiver-integrated impulse의 bilinear
이동 근사다. distortion warp, 깊이/가림에 따른 연속 PSF, polarization, 실제
PRNU/DSNU, 측정 기반 물리 불확도는 포함하지 않는다. 명시된 시간 장면과 제한된
trapped paired retained acquisition 외의 HDR/DCG 취득에는 이 source RI 계약이
연결되지 않았고 거부한다.
따라서 `physical_validation`은 `not_established`다.
