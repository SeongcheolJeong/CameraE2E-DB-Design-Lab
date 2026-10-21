# 소스 서브셀 광휘와 렌즈 PSF의 위치별 전달

`source_pixel_quadrature.model = "native_sensor_area_midpoint_retained_subcells_v1"`을 선택하면 3D Mitsuba 렌더러는 native 소스 셀의 2×2, 4×4, 8×8, 16×16 midpoint 광선 광휘를 평균하기 **전**에 `source-subcell-radiance.npy`로 보존한다. `source-subcell-mc-standard-error.npy`도 같은 좌표로 보존하고, 기존 `spectral-radiance.npy`와 오차 배열은 면적 가중 평균이다. 옵션이 없는 요청 및 기존 `native_sensor_area_midpoint_grid_v1` 결과는 그대로 셀 평균 경로를 따른다.

```json
{
  "source_pixel_quadrature": {
    "model": "native_sensor_area_midpoint_retained_subcells_v1",
    "grid_side": 2
  }
}
```

등록된 렌더→게시→Test→RAW/ISP 또는 저장 광휘→새 절대 투영→게시→Test→RAW/ISP 경로에서 Worker는 각 소스 서브셀의 분광 광휘에 승인된 절대 투영 계수를 한 번 곱한다. 그 다음 고정 PSF 또는 field PSF를 소스 서브셀 위치에서 평가하여 수신 픽셀로 보낸다. field PSF는 해당 위치의 기존 노드 보간을 이용한다. 입력은 투영 후 `W/m²/nm`, 서브셀 면적 가중치는 `1/grid_side²`, PSF는 수신 픽셀에서 이미 적분된 단위합 응답이다. 기본 모드에서는 상대 조도, 센서 방향 응답, EQE, 전기 readout을 기존 수신 단계에 각 한 번 적용한다. 등록된 `spatial_scene_v1`에서 [`source_position_ri_v1`](camera-platform-source-position-ri.md)을 선택하면 상대 조도는 분수 **소스** 위치에서 PSF 전에 적용하고, 수신 위치 상대 조도는 다시 곱하지 않는다.

현재 제공하는 **분수 소스 위치 응답은 근사**다. Lens에 저장된 PSF는 native **소스 셀 중심**에 대한 수신 픽셀 적분 impulse다. 그 응답을 위치별로 실제 측정하거나 광학 처방에서 직접 재계산한 자료가 아직 없으므로, 서브셀 위치에서는 그 이산 impulse를 2차원 선형 보간하여 이동한다 (`bilinear_receiver_integrated_impulse_v1`). 수신 개구 box filter를 다시 적용하지 않는다. 이 방법은 서브셀 광휘를 PSF 전에 잃어버리지 않지만, 실제 연속 위치·방향·깊이 결합 PSF 또는 수차 정확도를 입증하지 않는다. `continuous_scene_psf_integration`은 여전히 false다.

작업은 실행 전에 실제 경로 수 262,144, FFT 추정 작업 500,000,000 단위, 아티팩트 8 MiB, 기본 Runtime 입력 64 MiB로 제한한다. 초과하면 투영/RAW 대기열에 넣지 않는다. 각 서브셀 입력을 PSF에 FFT 합성한 다음 이미 적분된 impulse를 위치 보간하므로, 넓은 PSF halo도 수신 픽셀마다 모든 tap을 직접 순회할 필요가 없다. FFT의 미세한 음수 반올림은 허용 오차 안에서만 0으로 자르고 그 광량을 ledger에 기록한다. PSF+carrier halo를 요청했다면 분수 이동에 필요한 반경 1 native 픽셀을 기존 kernel 반경에 더한다. field PSF가 서브셀 극단 위치를 덮지 못하면 거부하며, 외삽하지 않는다.

RAW 결과의 `source_subcell_transfer`는 원본 서브셀 광휘 SHA-256, 렌더·투영 작업 ID, 격자와 전달 모델, 작업 상한을 기록한다. `source_subcell_irradiance.npy`에는 투영 후 서브셀 입력이 남는다. 기본 모드의 `flux_diagnostics`는 상대 조도 **전** 파장별 입력, 무한 이산 출력, 제공된 source-support crop, 물리 센서 안/밖 광량을 별도로 기록한다. Source RI 모드는 PSF 전 광량을 pre/post-RI로 별도 기록한다. 전 평면의 단위합·비음수 광량 보존을 검사한다. 이 보존은 **선택한 이산 근사 연산자**에 대한 검사이고, 연속 장면이나 실측 렌즈의 오차 상한은 아니다.

깊이·첫 물체 ID는 계속 셀 중심 광선의 보조 정보다. 서브셀별 깊이/입사각 변화는 현재 PSF 선택에 반영되지 않는다. 광학 꼬리 밖 광량, 연속 가림·움직임 노출 적분, 물리 PWM/센서 교정 및 실물 카메라 검증은 별도 과제다.
