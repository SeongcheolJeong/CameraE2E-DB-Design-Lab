# 동일 3D 장면의 두 광원 성분 렌더링

`SceneRenderCreate.light_component_id`는 기존 Scene에 배치된 **Light 자산 ID** 하나를
선택하는 opt-in 필드다. 선택하지 않으면 렌더러는 종전처럼 모든 광원을 켜며, 요청·렌더러
요약의 레거시 직렬화도 유지한다. 선택하면 면광원, 점광원, 환경광 중 해당 자산 ID가 설치된
모든 emitter만 켜고 나머지 emitter는 끈다. 형상, 재질, 카메라, 이동 위치와 가림은 어느
렌더에서도 그대로 유지한다. 광원 자산에 같은 ID를 여러 위치에 배치했다면 이 배치들은
하나의 성분으로 묶인다.

Assembly `conditions.light_id`는 **장면 전체**의 불변 `illumination_id`를 계속 가리킨다.
선택한 광원 ID로 교체하면 전체 장면과 카메라 계약이 달라지므로 제출 전에 거부한다.
렌더 요청에는 선택 ID·전체 Scene snapshot·Light 및 다른 자산 snapshot·카메라 조건·
시간·Mitsuba 실행 파일/adapter 해시가 고정된다. `renderer-summary.json`은 선택 Light
ID/자산 해시와 전체 조명 ID를 남긴다. Worker 실행, 캐시된 성공 작업 재사용, 게시 시
이를 재검사한다. 게시된 분광 이미지의 `render_job_snapshot`과 근거 ZIP에서 원본 선택을
다시 읽을 수 있다.

두 개의 독립적 3D 조명 성분을 시간축에 올리는 순서는 다음과 같다.

```python
primary_jobs = scene_motion_sampling.submit_renders(
    service, {**base_render, "light_component_id": primary_light_id}, times_ns
)
ambient_jobs = scene_motion_sampling.submit_renders(
    service, {**base_render, "light_component_id": ambient_light_id}, times_ns
)
# Worker에서 각 작업을 완료한 뒤 각 계열을 게시한다.
primary = scene_motion_sampling.publish_sequence(
    service, [job["id"] for job in completed_primary], name="3D PWM source"
)
ambient = scene_motion_sampling.publish_sequence(
    service, [job["id"] for job in completed_ambient], name="3D ambient source"
)
pair = scene_render.verify_light_component_sequences(
    service, primary["id"], ambient["id"]
)
```

각 출력은 기존 `timed_spectral_scene_v1`이며 동일한 파장·native 격자·crop·렌즈 절대
투과·장면 clock을 가진다. 따라서 기존 `unmodulated_scene_sequence_id` 경로에서 첫 번째
성분에만 작성된 PWM 게이트를 적용하고 두 번째 성분을 별도의 3D 주변광으로 적분할 수 있다.
두 시퀀스가 모두 등록된 3D 렌더 계열이면 Run 제출 사전검사가 이 쌍을 다시 검증하고,
`dual_3d_light_evidence`를 고정 작업과 결과 요약에 남긴다. 같은 광원 렌더를 두 계열로
재포장하거나 전체 광원 렌더를 성분으로 섞으면 실행 전에 거부한다. 독립 작성 영상면
주변광을 쓰는 이전 경로는 계속 허용하지만 이 3D 쌍 근거를 받지 않는다.
광원 `time_model`은 현재 `static`이므로 LED PWM 파형은 3D 경로 추적 내부에서 평균화하거나
추정하지 않고, 두 성분이 분리된 뒤 행별 노출 시간축에서 적용한다. 이는 각 시점의
조명 공간 분포가 고정되고 PWM이 그 광원 성분의 스칼라 세기만 변화시키는 근사다.

`verify_light_component_pair`는 정확히 두 Light 자산이 설치된 장면에서 두 완료 렌더가
선택한 광원을 빠짐없이 나누는지, 장면·카메라·Backend·시간·첫 충돌 깊이/객체 지도가
같은지 검사한다. 선택하지 않은 전체 광원 렌더도 선택적으로 대조할 수 있다.
`verify_light_component_sequences`는 두 등록된 시간 계열의 실제 렌더 snapshot을 다시
읽고 모든 knot를 쌍으로 검증해 재구성 가능한 pair digest를 낸다. 분광 irradiance의
두 계열은 독립적으로 보존되며 센서 입력 이전에 임의로 합산하지 않는다.

선형 장면의 **기대 복사휘도**는 광원별 합과 일치한다. 유한 Monte Carlo 표본의 전체
광원 렌더와 두 개별 렌더의 수치가 매 픽셀 정확히 일치한다는 보장은 없다. 독립 시험에서는
반사율 0의 두 면광원과 이동하는 검정 가림판을 사용해, 직접 발광의 독립 스펙트럼 해석값과
두 성분 합이 전체 광원 결과와 정확히 일치하는 특수한 결정론적 조건을 검증했다. 별도의
점광원/환경광 시험은 Lambertian 거리 및 균일 환경광 기준과 비교했다. 일반 재질의
다중 반사·간접광, 시간 knot 사이 가림, 실제 LED의 공간·시간 파형, 실물 정확도와
전체 불확실도는 검증되지 않았다.

검증 소스: [두 광원 직접/등록 시험](../tests/platform/test_scene_light_components.py) ·
[두 광원의 PWM·잔류 전하·공유 전하 RAW 통합 시험](../tests/platform/test_registered_dual_3d_pwm_trapped.py).

## 두 광원의 시간 격자 정밀화 진단

`scene_motion_sampling.compare_dual_light_refinement`은 같은 두 Light 자산을 고정한
4개 등록 시퀀스(성분별 coarse/fine)를 읽는다. Fine의 시각은 coarse의 양 끝과 모든
기존 시각을 포함하고, 공통 시각의 **동일 렌더 작업 ID/snapshot**을 재사용해야 한다.
다른 Scene, 카메라, Backend, clock, Light 선택이나 공통 knot의 재렌더링을 거부한다.

입력 `row_windows_ns`의 각 항목은 실제 native 센서 행 번호와 반열린 노출 구간이다.
예: `{"row": 12, "start_ns": 4_000_000, "end_ns": 8_000_000}`.
`waveform`은 같은 clock ID의 `camera_light_waveform_v1` PWM 계약이다. 이 PWM은 첫
번째 3D Light 성분에만, 작성된 low/high·주기·ON 길이·위상 그대로 적용한다. 두 번째
3D 성분은 단위 세기로 적분한다. 각 행에 대해 성분별 비게이트 분광 irradiance 적분,
첫 성분의 PWM 가중 적분, 마지막 합계의 coarse/fine 표본값 합·L1 차이·최대 차이를
보고한다. 기준면은 센서 앞 ideal image이며 단위는 표본 파장·픽셀당 `J/m²/nm`이다.
센서 QE·PSF·RAW·ISP 오차와 혼동하지 않는다.

```python
diagnostic = scene_motion_sampling.compare_dual_light_refinement(
    service,
    coarse_primary_id, coarse_ambient_id,
    fine_primary_id, fine_ambient_id,
    waveform=pwm_specification,
    row_windows_ns=[
        {"row": native_row, "start_ns": reset_end_ns, "end_ns": transfer_ns},
    ],
)
```

PWM 가중치는 각 선형 irradiance 구간과 PWM 경계의 정수 시간 모멘트로 계산한다.
검증 시험은 제품 적분기를 사용하지 않고 모든 PWM ON/OFF 경계를 열거한 뒤 선형
trapezoid를 합산한다. 이동 가림의 새 knot가 보고 차이를 바꾸는지, 위상 변경이 결과를
바꾸는지, 다른 장면·재렌더한 공통 시각·원본 artifact 변조가 거부되는지도 확인한다.
이는 **관측된 coarse/fine 차이**일 뿐 연속 움직임·가림 오차의 상한이나 실측 LED/
카메라 정확도 기준이 아니다. [독립 시간 적분 시험](../tests/platform/test_scene_dual_light_refinement.py).
