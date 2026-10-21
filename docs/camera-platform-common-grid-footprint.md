# Lens × Sensor 공통 각도 MTF의 픽셀 footprint 경계

`camerae2e_lens_sensor_quality_study_v2`의 `common_grid_object_angle_edge_mtf50`은 네 ISP 영상을 같은 **550 nm 기하 픽셀 중심 격자와 물체각 ROI**에서 재표본화한다. 이는 각도 좌표와 측정 영역의 차이를 줄인 합성 edge 진단이다. 이미 촬영된 RAW에는 렌즈 PSF와 센서의 공간 적분이 들어 있으므로, ISP 영상을 같은 격자에 놓는 것만으로 네 카메라의 유효 수광 footprint가 같아지지 않는다.

결과의 `model_550nm_point_cell_footprint`는 각 소스의 실제 화질 ROI 안쪽에서 보존된 왜곡 반영 중심 지도의 중앙 차분을 계산한다. 행·열 방향 각도 길이와 두 벡터의 외적 면적을 슬롯별 min/median/max로 기록하고, 네 슬롯의 median 최대/최소 비율을 보고한다. 행 0.001° × 열 0.002°와 행 0.002° × 열 0.001°는 면적이 같아도 방향별 샘플 폭은 다르다. 그래서 면적과 두 축을 모두 확인하며 `proxy_within_one_percent_all_axes_and_area`는 이 **기하 proxy**의 1% 이내 여부만 나타낸다. 지도 누락·비유한 값·접힌 셀은 이 진단과 공통 격자 MTF를 차단한다.

이 수치는 픽셀 중심을 이은 셀의 기하학적 면적이다. 실제 광전자 형성에는 파장별 렌즈 PSF, 픽셀 면적·샘플링 커널, CFA·QE, 입사 방향 응답이 영향을 준다. 현재 저장 RAW의 뒤쪽에서 그 항목을 역추정하거나 동일하게 만들 수 없다. 따라서 `actual_pixel_integration_or_psf_footprint_compared=false`, 기존 `pixel_integrated_effective_area_matched=false`, `product_ranking_allowed=false`는 기하 proxy가 1% 안에 들어도 유지한다. 공통 격자 MTF 수치는 같은 좌표에서의 모델 민감도이며 실측 시스템 MTF가 아니다.

다음 구현 경계는 **소스 각도·파장 → 노이즈·ADC 이전 평균 광전자**의 국소 전달 응답을 후보마다 생성·보존하는 것이다. 해당 응답은 동일한 dense scene grid, Lens revision/field/PSF, Sensor revision/CFA/QE/공간 적분, 에너지 정규화와 선형·비포화 조건에 묶여야 한다. 그 뒤 같은 물체각 ROI에서 네 응답의 유효 지지 영역과 파장별 가중치를 비교할 수 있다. 저장된 기하 PSF의 회절·광량, 센서 CRA 응답, 제품 readout이 검증되기 전에는 그 전달 응답 또한 모델 내부 근거에 한정된다. 이 연결 없이 공통 격자 후처리만으로 제품별 화질 순위를 해제해서는 안 된다.
