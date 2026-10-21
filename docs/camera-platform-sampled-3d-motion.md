# 등록형 3D 장면의 시각별 렌더링

`Scene`의 `clock`과 물체의 `motion`을 선언하면, 같은 불변 3D 자산을 서로 다른
시각에 Mitsuba로 렌더링할 수 있다. `SceneRenderCreate`의 `render_clock`과
`render_time_ns`는 등록형 촬영의 분광 영상 knot가 사용하는 **동일한 상대 ns 좌표**다.
절대 시각이나 하드웨어 취득 시각으로 변환하지 않는다.

```json
{
  "clock": {
    "clock_id": "authored-3d-motion",
    "unit": "ns",
    "reference": "relative",
    "origin": "Authored simulation zero; not a hardware timestamp"
  },
  "instances": [{
    "geometry_id": "IMMUTABLE_GEOMETRY_ID",
    "material_id": "IMMUTABLE_MATERIAL_ID",
    "to_world": [[1,0,0,0],[0,1,0,0],[0,0,1,1],[0,0,0,1]],
    "motion": {
      "model": "linear_translation_v1",
      "velocity_m_s": [0.25, 0, 0]
    }
  }]
}
```

`to_world`는 clock의 `t=0`에서의 자세이고, 속도는 월드 좌표 m/s다. 렌더
시각 `t_ns`에서 평행 이동은 정확히 선언한 시간 좌표를 사용해
`velocity_m_s × t_ns × 10^-9`로 계산한다. 카메라 `pose.motion`에도 같은
`linear_translation_v1`을 선택할 수 있으며, 카메라 원점과 목표점에 동일한
이동량을 적용해 시선 방향을 유지한다. 회전·변형·카메라 회전은 이 계약에 없다.

각 렌더 요청은 scene clock과 같은 `render_clock` 및 정수 `render_time_ns`를
함께 제공한다. 움직임이 있는 Scene은 둘 다 빠진 요청을 거부하며, 다른 clock이나
안전 범위를 벗어난 이동량도 Worker 제출 전에 거부한다. 완료된 렌더의 frozen
request에는 원래 Scene revision, 시각, clock, 해당 시각의 기하/카메라 자세와
해석식 근거가 들어간다. 결과 `renderer-summary.json`에도 같은 `scene_motion`
근거를 남기고, 발행된 분광 영상에는 원본 렌더 job snapshot을 보존한다.

발행된 분광 영상을 `timed_spectral_scene_v1`에 등록할 때, 해당 렌더가
시각을 명시했다면 이미지의 `render_time_ns`와 `render_clock`이 knot 시각과
sequence clock에 **정확히** 일치해야 한다. 저장된 순간 분광 광휘를 다른 렌즈
광량 조건으로 재투영해 만든 이미지도 원본 렌더의 시각을 물려받으며, 재투영을
새 시각의 3D 렌더로 해석할 수 없다. 그 뒤 기존 `linear_irradiance`
계약은 매듭 사이의 **영상면 조사도**를 선형 보간해 행별 노출 창에 적분한다.
Mitsuba는 각 knot에서 실제 3D 형상·재질·광원의 깊이/가림/분광 광휘를 다시
계산한다. 따라서 장면이 움직이면 PSF·픽셀 광학·전자 수집·readout·정수 RAW·ISP
까지 같은 경로로 전달된다.

`scene_motion_sampling`은 이 경계를 실제 중간 시각 렌더로 좁히는 등록형 경로다.
`plan_times(start_ns, end_ns, max_gap_ns, required_times_ns=...)`는 정확한
정수 ns 격자를 만든다. 최대 65개 시각으로 제한하고, 지정한 필수 시각을
포함하면서 인접 시각 간격이 `max_gap_ns`를 넘지 않게 한다. 그 시각들을
`submit_renders(service, base_request, times_ns)`로 같은 Scene·clock·카메라·렌즈
요청에 제출한다. Worker가 완료한 job ID를 시각순으로
`publish_sequence(service, render_job_ids, name=...)`에 전달하면 실제 렌더
결과가 동일한지 확인하고 발행된 분광 영상으로 `timed_spectral_scene_v1`을
만든다. 일반 등록형 Control/Test/Worker가 이 시퀀스를 받아 행별 노출 적분,
PSF, 픽셀 수집, RAW와 ISP까지 계산한다.

두 개의 중첩 시각 격자로 만든 시퀀스는
`compare_refinement(service, coarse_id, fine_id, windows_ns=...)`로 비교한다.
고운 격자는 성긴 격자의 **같은 완료 렌더 job**을 포함해야 한다. 결과는 전체
시간 구간과, `windows_ns`가 주어졌다면 호출자가 지정한 창의 분광 조사도 적분
변화량(J/m²/nm)을 보고한다. `windows_ns`는 등록된 Control/Run과 자동으로
결합되지 않으므로 결과만으로 그 창이 실제 행 노출 창임을 증명하지 않는다.
등록형 테스트는 저장된 행 시작·끝 배열에서 창을 가져와 비교한다. 이는 계산
격자를 좁혀 관측된 변화량이며 실제
가림 오차의 상한이 아니다. Monte Carlo 광선 잡음도 변화량에 포함된다. 등록형
테스트는 이동하는 불투명 물체에 대해 0·80 ms 양 끝 렌더와 20 ms 간격의 실제
3D 렌더 시퀀스를 비교하며, 행 노출 적분·전자 수·정수 RAW가 달라짐을 확인한다.
같은 장면을 10 ms로 다시 조밀화했을 때, 전체 시간 구간 조사도 적분의 관측
변화량이 앞선 0·80→20 ms 조밀화보다 작아지는 것도 확인한다. 이는 해당
테스트 장면에서의 수치 수렴 증거이며 일반적인 오차 보증은 아니다.

여전히 선택한 시각 사이의 연속 형상과 가림은 직접 레이트레이싱하지 않는다.
빠른 가림 변화가 한 간격 안에 들어가면 조밀화 비교에서도 놓칠 수 있다.
`first-hit` 깊이·물체 인덱스는 각 knot의
중심 광선 진단이고 노출 동안의 물체 점유율이 아니다. 이 단계에서 별도 HDR
합성은 움직임 때문에 자동으로 활성화하지 않으며, no-fusion 원본 RAW 획득이
정직한 기본 경계다. 현재 PSF는 고정된 Lens 응답이므로 깊이 변화에 따른
PSF 재계산도 하지 않는다. 온도/전기 잔류, 시간 변화하는 점광원/PWM의 독립
분광 변화, 카메라/물체 회전과 정확한 연속 가림 적분, 실물 측정 기반 오차
경계는 추가 기능과 검증이 필요하다. `physical_validation`은
`not_established`다.
