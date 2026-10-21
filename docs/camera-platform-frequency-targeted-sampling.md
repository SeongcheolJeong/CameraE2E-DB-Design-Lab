# 목표 주파수 기반 Scene·PSF 표본화

`lens_sensor_frequency_target`는 저장된 Lens revision, Sensor run, 등록 active mode,
고정 FOV와 센서 ROI를 묶어 **수치 표본화 민감도**를 검사하는 별도 Job이다.
기존 `/api/v1/lens-sensor-registered-roi-disk-raw`의 입력과 결과 의미는 바꾸지
않는다. 새 작업은 `/api/v1/lens-sensor-frequency-target/template`에서 현재
요청 계약을 받고, `POST /api/v1/lens-sensor-frequency-target`으로 제출한다.
공통 `/api/v1/jobs/{id}` 조회와 전용 조회·목록·취소·export가 가능하다.
SDK는 `lens_sensor_frequency_target_template()`, `submit_lens_sensor_frequency_target()`,
`wait_lens_sensor_frequency_target()`, `lens_sensor_frequency_target()`을 제공한다.
큰 **가상 Scene에서 작은 ROI만** 평가하는 작업에는 별도의 명시적
`/api/v1/lens-sensor-frequency-target/template/extended-sparse-roi`가 있다.
기본 v1과 기존 등록 ROI RAW 요청의 좌표 정책은 바뀌지 않는다.

## 설정과 실행

template의 `registered_roi`에는 기존 등록 ROI RAW 요청과 같은 Lens/Sensor/active
mode ID, FOV, 센서 ROI, Scene 크기, 광량, 노출, 타일 크기, 물리 가정 승인값을
채운다. `sampling_goal`의 모든 키가 필수이며 알 수 없는 키는 거부한다.
이 작업은 잡음 전 광전자만 계산하므로 `capture.noise_flag=0`이 필요하고,
호환 입력의 `capture.seed`는 고정·기록되지만 이 관측량에는 쓰이지 않는다.

| 설정 | 의미 |
| --- | --- |
| `mode` | `explicit`: 지정한 Scene shape에서 시작한다. `adaptive`: 목표 대역과 고정 FOV에서 시작 shape를 계산한다. |
| `observable` | 현재 `pre_readout_mean_electrons`만 지원한다. RAW의 잡음·gain·ADC 또는 ISP 성능 판정이 아니다. |
| `frequency_direction` | 현재 영상면 열 방향 `image_plane_column_x_right`만 지원한다. |
| `target_fraction_native_nyquist` | 센서 열 피치의 `500 / pixel_pitch_um` cycles/mm에 대한 시작 격자 목표. 기본값 0.9. |
| `minimum_scene_samples_per_target_period_seed` | 목표 주기당 Scene 표본의 **탐색 시작값** 2~16개, 기본 4개. 수렴 보증값이 아니다. |
| `evaluation_fractions_native_nyquist` | 증가하는 2~16개의 **이산** 평가 주파수. 모두 Nyquist보다 작고 목표 이하이어야 한다. 사이의 연속 대역을 검증하지 않는다. |
| `max_refinements` | `adaptive`: 2~4회의 2배 격자 시도. `explicit`: 0 또는 2~4회. 수렴에는 연속 두 번의 통과가 필요하다. |
| `mean_electron_full_scale_normalized_max_delta_tolerance`, `shift_response_full_scale_normalized_max_delta_tolerance` | 광전자·에지 이동 응답의 최대 절대 변화량을 두 위상 ROI에서 관측된 최대 광전자 신호로 나눈 허용값. 어두운 픽셀별 상대 오차가 아니다. |
| `mtf_absolute_tolerance` | 지원되는 단색 모델의 선언된 이산 주파수 점에서 MTF 절대 변화 허용값. |
| `scene_rasterization` | `cell_area_slanted_edge`: 매 단계에 같은 연속 에지를 셀 면적으로 적분해 다시 생성한다. 저장 raster의 확대가 아니다. |
| `psf_resolution_mode` | `stored_geometric_only` 또는 `pinned_same_ray_retrace_diagnostic`. 후자는 재추적 PSF를 동일 Scene 격자의 카메라 계산에도 적용해 민감도를 본다. |
| `psf_target_pitch_um` | 명시 재추적에서는 양수 µm. adaptive의 `null`은 마지막 실행 OI 격자 간격을 사용한다. 저장 PSF 모드에서는 `null`이어야 한다. |
| `optical_tile_estimated_working_budget_bytes` | 타일 작업량 추정 정책. 256 MiB~8 GiB에서 명시 가능, 기본 512 MiB. 큰 격자는 명시적으로 예산을 올려야 하며 하드웨어 한계나 peak RSS 보증이 아니다. |
| `alias_probe_grid_seed_multiple_native_nyquist` | 선택한 Nyquist 초과 대역을 시작 격자 **계획**에만 반영한다. 결과의 `alias_probe_executed`는 `false`이며 alias 응답 판정은 제공하지 않는다. |

예를 들어 template을 받은 뒤 다음처럼 시작한다. 기존 DB ID와 등록 active mode는
사전에 선택·생성해야 한다.

```python
spec = client.lens_sensor_frequency_target_template()["specification"]
roi = spec["registered_roi"]
roi["lens_revision_id"] = lens_revision_id
roi["sensor_revision"] = {"identifier": sensor_report_code, "run_id": sensor_run_id}
roi["active_mode_declaration_id"] = active_mode_id
roi["sensor_roi_bounds_row_col_exclusive"] = [1020, 1028, 7000, 7008]
roi["analytic_scene"].update(
    shape_row_col=[4096, 4096], horizontal_fov_deg=2.0, edge_offset_deg=0.0
)
roi["accept_approximate_lens_radiometry"] = True
roi["acknowledge_unverified_mode_and_uniform_sensor_response"] = True
spec["sampling_goal"]["psf_resolution_mode"] = "pinned_same_ray_retrace_diagnostic"
spec["sampling_goal"]["optical_tile_estimated_working_budget_bytes"] = 1024**3
queued = client.submit_lens_sensor_frequency_target(spec)
completed = client.wait_lens_sensor_frequency_target(queued["id"])
report = completed["result"]["summary"]
```

이 설정은 등록 센서의 **작은 중앙 ROI**에 대한 예시일 뿐이다. 목표 물체각 FOV
전체의 카메라 해상력이나 제품 순위로 확대 해석할 수 없다. 저장 모델이 없는
환경에서는 template과 validation만 사용할 수 있다.

## 계산·판정 경계

입력은 동일한 Lens/Sensor revision, 등록 좌표, 광량, 노출, FOV, ROI, 파장,
에지 각도와 이동량으로 고정한다. 각 단계는 Scene 표본 간격을 절반으로
줄이고 CameraE2E strict 광학 경로에서 저장 기하 PSF를 그 OI 간격으로 다시
샘플링해 convolution한다. 이어 센서 수광·QE 모델의 **잡음 전 평균 광전자**를
계산한다. 따라서 이 비교가 통과하면 *Scene/OI 격자와 실행 PSF 보간을 함께
바꾼* 수치 결과의 안정성이다. Scene 단독 수렴이나 저장 PSF 원천의 수렴이
아니다. 각 단계에는 Scene/OI/원본 PSF/센서 pitch, 소스 식별 해시, 두 위상의
광전자 해시, 자원 preflight, 변화량과 중단 사유를 보존한다. 결과 JSON은
content-addressed artifact로 저장된다.

단색 균일-site 센서에서만 48×48 이상이고 에지 직선성·대비·지원 주파수가
확인되면 ISO12233 기반 수치 SFR 곡선의 **선언된 이산 주파수 점** 차이를 본다.
곡선 사이 전체 연속 대역의 오차 상한은 계산하지 않는다. Bayer mosaic는
채널별 유효 Nyquist와 demosaic 계약이 없으므로 `frequency_target_points_status`를
`inconclusive_mosaic_channel_contract`로 둔다. 8×8 RAW ROI의 광전자 안정성은
관측할 수 있지만 Nyquist 대역 MTF 합격 판정은 할 수 없다. Nyquist 정확히
끝점, 그 이상의 alias 응답, 복소 위상 전달과 task perception도 이 Job의
합격 항목이 아니다.

재추적을 선택하면 고정 처방의 **같은 광선 교점**을 더 작은 PSF bin으로
다시 적분한다. 저장된 정확한 한 field-height·파장 node에서 복소 OTF,
centroid와 광선 유지율을 비교한다. 실행 Scene pitch와 재추적 PSF pitch가
일치할 때는 같은 고정 격자에서 저장/재추적 PSF의 두 광전자 위상 배열도
비교한다. 이는 histogram bin 민감도 및 해당 수치 카메라 응답 차이다.
명시한 PSF pitch가 실행 Scene pitch와 달라도 독립적인 PSF node 진단은
수행하고, 같은 격자 카메라 비교만 미결론으로 둔다.
광선 밀도·회절·CRA×픽셀 각도 응답의 **독립적인 PSF 원천 수렴**은 아니며,
단일 node OTF가 위치 보간·회전·다파장 주변부 kernel 전체를 검증하지도
않는다. 이 축은 `psf_source_resolution_qualified=false`로 유지한다.

필요한 격자가 가상 Scene 축·셀 정책 또는 tile 작업량 정책을 넘거나,
ROI·에지가 주파수 판정에 충분하지 않으면 실행은 `inconclusive_*`와 마지막
유효 단계·정책 근거를 반환한다. 정책 한도는 장비의 물리 메모리 경계가 아니다.
기본 v1 Job의 Worker 제한은 1,800초이며 tile 사전 추정이 peak RSS나
완료 시간의 보증은 아니다.

현재 고정 증거의 P0248 광각 렌즈 × DEP-2209-801, 42° FOV 사례에서는
1.22 µm 센서 Nyquist가 약 409.84 cycles/mm이고 원본 기하 PSF 간격이 약
0.3821 µm다. 시작 격자 2304×15750에서 연속 두 refinement에 필요한 셋째
격자는 536,870,912 가상 셀 정책을 넘으므로 수렴은 **미결론**이다. 관련
[읽기 전용 계획](../reports/camera_platform/mtf-support-20260928/registered-roi-frequency-target-dev45-pinned-planning-only.json)을
실제 RAW/MTF 수렴 결과로 간주하지 않는다.

[중앙 ROI의 현재 계약 재추적 진단](../reports/camera_platform/frequency-target-20260929/validation-evidence-current.json)은
다른 고정 렌즈 처방에서 1 GiB 타일 정책 아래 두 격자를 촬영하고, 두 번째
격자(0.6092 µm)에서 같은 광선의 PSF bin 재적분을 적용했다. 저장/재적분 PSF
사이의 최대 광전자 차이를 전체 관측 최대 신호로 정규화한 값은 약 0.00676,
에지 이동 응답의 같은 정규화 차이는 약 0.00336이었다. 이는 어두운 픽셀별
상대 오차를 보증하지 않는다. 세 번째 Scene 격자는 타일 정책에서 막혔고 Bayer 주파수
대역과 PSF 원천 수렴도 미결론이다. 이 값은 제품 정확도나 MTF 순위가 아니다.

같은 저장 모델의 1° 중앙 ROI에서는 504²→1008²→2016² 격자가 계획됐다.
64² 소스 타일의 세 번째 격자 추정치 약 1.44 GiB는 1 GiB 정책에 막혔다.
2 GiB 정책에서는 사전 검사를 통과했지만 420초 시험 제한 안에 계산이
끝나지 않았다. [128² 타일 실행](../reports/camera_platform/frequency-target-20260929/tile128-three-grid-model-only.json)은
같은 고정 Scene·렌즈·센서에서 세 격자를 약 194초에 완료했다. 앞선 두
격자의 광전자 해시는 64² 타일과 위상별로 정확히 같았다. 하지만 마지막
광전자 최대 변화량은 전체 신호 기준 약 1.72%로 설정한 1% 허용값보다 컸고,
연속 두 번의 통과도 없다. 따라서 **세 격자를 계산한 것과 수렴한 것은
다르다**. Bayer 주파수 지점 판정도 계속 미결론이다. 타일 크기 변경은
성능에 실질적이지만, 현재 strict 위치별 PSF의 타일별 반복 비용과
광각 전체 화각에서 필요한 격자 규모는 후속 개선 대상이다.

[PSF 준비 캐시를 적용한 같은 세 격자 Worker 재실행](../reports/camera_platform/frequency-target-20260929/p0014-frequency-three-grid-post-cache-proof.json)은
이전 실행과 각 격자의 두 위상 광전자 배열 해시가 정확히 같았다. 실행 시간은
약 194초에서 102초로 줄었고, 자식 프로세스 최대 RSS는 약 868 MB였다.
이는 이 중앙 ROI·저장 모델에서의 속도와 출력 동일성 증거이며, 전체 화각의
처리량이나 메모리 상한을 보증하지 않는다. 마지막 수치 변화와 수렴 미결론
상태도 그대로다. 공유 PSF 준비 캐시의 정책 상한 32 MiB는 타일 사전 검사에
포함하지만, 그 값은 전체 프로세스의 추가 RSS 상한이 아니다.

[넷째 격자의 단독 사전 검사](../reports/camera_platform/frequency-target-20260929/fourth-grid-preflight.json)는
동일한 1° FOV·저장 렌즈·센서·8×8 ROI에서 4032² Scene 간격 0.15228 µm와
2059² 실행 PSF 커널을 계산했다. 128² 소스 타일의 커널 관련 정책 추정은
4,467,057,664 byte, 준비 캐시 33,554,432 byte를 포함한 전체 작업 추정은
5,306,126,048 byte로 시험에서 선언한
2 GiB를 넘는다. 그래서 광전자 촬영 전에
`blocked_working_memory_policy`로 중단됐다. 이 수치는 실제 peak RSS나
하드웨어 메모리 한계가 아니다. 별도의
[6 GiB 명시 설정 사전 검사](../reports/camera_platform/frequency-target-20260929/fourth-grid-opt-in-preflight.json)는
`ready_under_declared_policy`였지만 촬영은 하지 않았다. 넷째 격자를 실행하더라도 두 번 연속 1%
통과 여부는 별도로 확인해야 한다.

[4032² 전체 Worker 시도](../reports/camera_platform/frequency-target-20260929/p0014-frequency-four-grid-worker-proof.json)는
6 GiB 선언 정책과 4 GiB 관측 RSS 중단 감시 아래 시작했다. 692초 시점까지
관측 RSS 최대치는 약 1.31 GB였고 CPU 계산이 계속됐지만, 실행 중 다른 Job의
소스 계약이 변경돼 종료 시 환경 지문 검사가 실패할 위험이 생겼다. 이 시도는
안전하게 중단했으며 결과 artifact가 없다. 따라서 넷째 격자의 광전자 변화율,
앞선 세 격자와의 해시 일치, 수렴 여부는 **미평가**다. 이 관측치는 4032²의
최대 메모리나 완료 시간 증거가 아니다.

소스를 고정한 [설치 wheel의 네 격자 Worker 재실행](../reports/camera_platform/frequency-target-20260929/p0014-frequency-four-grid-installed-v2-proof.json)은
504²→1008²→2016²→4032²를 472초에 완료했고, 자식 프로세스의 최대 관측
RSS는 약 1.48 GiB였다. 앞의 세 격자 광전자 해시는 이전 실행과 정확히
일치했다. 마지막 2016²→4032² 변화량은 전체 관측 신호 기준 평균 광전자
0.326%, 에지 이동 응답 0.115%로 각각 선언한 1%·2% 이내였다. **연속
통과는 한 번뿐이므로 전체 상태는 `inconclusive_refinement_limit`**다.
Bayer 8×8 ROI의 주파수 지점 판정은 `inconclusive_mosaic_channel_contract`다.
6 GiB 명시 정책에서 선택한 PSF 준비 캐시 상한은 256 MiB이며, 이 전체
Worker 결과에는 실제 cache 적중·peak 사용량이 보존되지 않았다. 실행 시간
차이를 캐시만의 개선 효과로 귀속하지 않는다.
[동일 4032² 광학 격자의 부분 입력 비교](../reports/camera_platform/frequency-target-20260929/psf-cache-p0014-partial-4032-optical-roi-benchmark.json)는
관련 소스 타일 289개 중 중앙 16개만 실제 장면으로 채워 32 MiB와 256 MiB
상한을 비교했다. 출력 바이트 해시는 같고 광학 ROI 시간은 82.1→55.9초,
cache hit는 0→293회, 관측 RSS는 약 941→1182 MB였다. 이는 해당 부분
입력의 캐시 효과이며 네 격자 Worker 전체의 속도나 정확도를 증명하지 않는다.

같은 2배 정련을 한 번 더 적용한 8064² 격자의
[정책 사전 검사](../reports/camera_platform/frequency-target-20260929/fifth-grid-policy-preflight.json)는
OI 간격 약 0.0761 µm에서 실행 커널이 보수적으로 4115² 셀이라고 계산한다.
현재 PSF 축 4097·셀 800만 정책에 걸려 `blocked_psf_axis_budget`이며,
해당 격자의 계산이나 정확도 결과는 없다. 따라서 4032² 단계에서 1%를 한 번
통과하더라도 현 정책으로는 다음 독립 단계의 연속 통과를 확인할 수 없다.

동일한 1° 실험을 다시 구성하려면 위 예시의 Lens/Sensor/active mode를 고정하고
`roi["analytic_scene"]["horizontal_fov_deg"] = 1.0`,
`roi["source_tile_shape_row_col"] = [128, 128]`,
`spec["sampling_goal"]["optical_tile_estimated_working_budget_bytes"] = 2 * 1024**3`
으로 설정한다. 이 조합도 1% 목표 통과를 보장하지 않는다.

### 주변부 작은 ROI를 위한 확장 좌표 정책

기본 v1의 가상 Scene 정책은 축 65,536·셀 536,870,912에서 멈춘다.
`camerae2e_lens_sensor_frequency_target_study_v2`는 오직 이 표본화 Job에서
`sampling_goal.coordinate_policy="extended_sparse_roi_128k_v1"`를 명시적으로
선택한다. 가상 Scene 축 131,072·셀 2,147,483,648까지 **계획과 작은 ROI
타일 실행**을 허용한다. 이 값은 소프트웨어 정책이며 장비 메모리 한계나
전체 프레임 실행 가능성의 증거가 아니다. v2 Worker 기본 시간 제한은
3,600초다. template의 정확한 키 집합을 유지해야 하며 v1 요청에
`coordinate_policy`만 추가하는 방식은 거부된다.

```python
spec = client.lens_sensor_frequency_target_extended_sparse_roi_template()["specification"]
roi = spec["registered_roi"]
roi["lens_revision_id"] = lens_revision_id
roi["sensor_revision"] = {"identifier": sensor_report_code, "run_id": sensor_run_id}
roi["active_mode_declaration_id"] = active_mode_id
roi["sensor_roi_bounds_row_col_exclusive"] = [1020, 1028, 13920, 13928]
roi["analytic_scene"].update(
    shape_row_col=[2048, 16000], horizontal_fov_deg=30.0,
    edge_offset_deg=14.047293808135992,
)
roi["source_tile_shape_row_col"] = [128, 128]
roi["accept_approximate_lens_radiometry"] = True
roi["acknowledge_unverified_mode_and_uniform_sensor_response"] = True
spec["sampling_goal"]["mode"] = "explicit"
spec["sampling_goal"]["max_refinements"] = 3
spec["sampling_goal"]["optical_tile_estimated_working_budget_bytes"] = 8 * 1024**3
queued = client.submit_lens_sensor_frequency_target(spec)
finished = client.wait_lens_sensor_frequency_target_extended_sparse_roi(queued["id"])
```

위 값은 P0014·DEP-2209-801의 저장된 Lens/Sensor와 등록 active mode가
이미 선택된 경우의 **주변부 시험 구성**이다. 다른 조합에서는 edge offset·
ROI가 실제 센서 위치 및 저장 광학 field support와 맞는지 다시 검사해야 한다.
오프축에서는 같은 각도 차이가 같은 영상면 거리가 아니므로, 위상 에지 이동은
`atan(tan(edge_offset)+requested_tangent_plane_shift/image_distance)-edge_offset`으로
계산한다. 이번 실행의 요청값은 1.22 µm native pixel의 0.25배인
**undistorted tangent plane 0.305 µm**이고 실행값도 0.305 µm였다.
렌즈 왜곡을 지난 ROI에서의 정확한 국소 이동량까지 검증한 것은 아니다.
[보정 전 세 격자 실행](../reports/camera_platform/frequency-target-20260929/p0014-outer-roi-30deg-three-grid-pre-shift-fix-proof.json)은
실제 tangent-plane 이동이 약 0.324094 µm로 목표와 달랐다. 이 결과의
위상 응답을 보정 후 실행과 같은 0.25 pixel 조건의 연속 정련 증거로 합치지 않는다.

[네 격자 Worker 증거](../reports/camera_platform/frequency-target-20260929/p0014-outer-roi-30deg-four-grid-proof.json)는
고정 30° FOV·8×8 주변부 ROI에서 2048×16000 → 4096×32000 →
8192×64000 → 16384×128000 가상 Scene을 실행했다. 각각의 Scene/OI 및
실행 PSF convolution 간격은 약 1.178255 → 0.589128 → 0.294564 →
0.147282 µm였다. 에지는 매번 같은 연속 장면에서 셀 면적 적분으로
다시 만들었고, PSF는 **같은 저장 원본**을 해당 실행 격자로 재표본화했다.
[앞의 세 격자 사전 검사](../reports/camera_platform/frequency-target-20260929/p0014-outer-roi-30deg-three-grid-preflight.json)와
[넷째 격자 사전 검사](../reports/camera_platform/frequency-target-20260929/p0014-outer-roi-30deg-fourth-grid-model-dependent-preflight.json)는
각각 정책 통과를 기록했다. 사전 검사의 작업량 추정은 실제 RSS의 상한이 아니다.

| 이전 격자 → 현재 격자 | 전체 신호 기준 최대 평균 광전자 변화 | 전체 신호 기준 에지 이동 응답 변화 | 연속 허용값 통과 수 |
| --- | ---: | ---: | ---: |
| 2048×16000 → 4096×32000 | 7.90066% | 0.81814% | 0 |
| 4096×32000 → 8192×64000 | 0.78375% | 0.27424% | 1 |
| 8192×64000 → 16384×128000 | 0.18908% | 0.10322% | 2 |

선언한 평균 광전자 1%·에지 이동 응답 2% 허용값을 마지막 두 정련이
연속 만족해 `mean_phase_status`는
`numeric_joint_scene_oi_grid_mean_phase_converged_model_only`다. 실제
Worker 시간은 약 216.9초, 자식 프로세스 최대 관측 RSS는 약 1.239 GB였다.
8 GiB 감시 임계값과 정책 예산은 하드웨어 상한이 아니다. 마지막 가상
Scene은 약 21억 셀이지만 해당 작은 ROI에 필요한 타일만 처리했다. 이를
30° 전체 영상 촬영의 시간·메모리 또는 full-frame RAW 검증으로 해석하면
안 된다.

이 통과는 **Scene/OI 격자와 실행 PSF 재표본화를 함께 바꾼 잡음 전 평균
광전자·에지 이동 응답**의 수치 안정성에만 해당한다. Bayer 8×8 ROI의
`frequency_target_points_status`는 계속
`inconclusive_mosaic_channel_contract`이며 전체 실행 상태도
`inconclusive_frequency_mosaic_channel_contract`다. 따라서 400 cycles/mm
근처나 Nyquist까지의 시스템 MTF, 연속 대역, alias 응답은 여전히 판정하지
못한다. 원본 기하 PSF의 동공 광선·histogram 밀도 수렴, 회절, 픽셀 각도
응답, 왜곡 후 국소 위상 이동, 실물 정확도도 별도 검증이 필요하다.

저장 PSF는 회절과 검증된 절대 광량·CRA 결합이 없는 기하 ray histogram이다.
등록 active mode와 균일한 sensor response도 공학적 선언이다. 따라서 결과의
`physical_mtf_comparison_allowed`, `product_ranking_allowed`,
`physical_accuracy_qualified`는 모두 `false`다.

## 독립 Lens 기하 PSF 동공 밀도 진단

`lens_geometric_pupil_density`는 위의 Scene·센서 ROI Job과 별개의 작업이다.
저장된 Lens DB revision의 **한 시야·한 파장 PSF node**에서 동공 ray 격자를
더 촘촘히 해, 지정한 이산 공간 주파수의 복소 OTF와 유효 광선 비율이
안정되는지 검사한다. Scene 생성, PSF convolution, 센서 적분, RAW, ISP는
실행하지 않는다. 따라서 이 결과는 위 `lens_sensor_frequency_target`의
Scene/OI 표본화 수렴을 대신하지 않는다.
현재 신규 요청 계약은 `camerae2e_pinned_geometric_pupil_density_study_v2`다.
기존 v1 결과는 원본 그대로 조회할 수 있지만 v2의 원본 밀도 적합 판정을
소급하지 않는다.

제출에는 PSF asset과 재현 가능한 reviewed prescription이 연결된 **기존 Lens
DB revision UUID**가 필요하다. 로컬 처방 파일 경로나 사양만 있는 렌즈는
입력이 아니다. 제출 시 렌즈 DB·처방·PSF 원본 해시를 고정하고, Worker가
계산 전후에 다시 확인한다. Worker는 저장 PSF를 만든 원래 동공 격자의
광선 성공 mask·chief ray·histogram이 재추적 결과와 일치하는지 먼저 검사한다.
이 재현 검사가 실패하면 더 높은 ray 밀도를 근거로 삼지 않는다.

| 요청 항목 | 지원 계약 |
| --- | --- |
| `field_index` | 요청값은 0~6. 실제로는 선택한 PSF asset에 **저장된** field index여야 한다. 시야 위치를 보간하지 않는다. |
| `wavelength_nm` | 입력 범위 100~2000 nm. 저장된 파장 node와 **정확히 일치**해야 하며 파장 보간은 없다. |
| `higher_pupil_samples` | 원본보다 큰 정수 2~4개를 오름차순으로 지정한다. 각 값은 3~257. 원본 격자도 첫 비교 단계에 포함된다. |
| `target_frequency_cycles_per_mm_row_col` | 행(y-up)·열(x-right) 각각 1~64개의 증가하는 값, 각 값은 (0, 5000] cycles/mm. **지정한 이산 점**만 검사하며 연속 대역·센서 Nyquist·alias 응답을 인증하지 않는다. |
| `maximum_complex_otf_delta`, `maximum_admitted_fraction_delta` | 각 단계의 복소 OTF 절대 변화와 유효 광선 비율 변화 허용값. 각각 0.000001~0.1이며, 연속 두 refinement가 모두 통과해야 안정 판정이다. |
| `max_total_requested_pupil_rays`, `soft_timeout_s` | 총 요청 광선 1~50,000개와 solver soft timeout 1~300초. 기본 Worker 제한은 420초다. 이 예산들은 정책이며 하드웨어 메모리 경계가 아니다. |

이 Job의 route가 로드된 서버에서 SDK는 현재 template을 읽고, 같은 요청을
제출·대기·조회할 수 있다. 아래
P0014 ID는 [실행 증거](../reports/camera_platform/frequency-target-20260929/p0014-pupil-density-worker-proof.json)의
고정 revision 예시다. 다른 렌즈에서는 해당 asset의 실제 field·파장 node와
원본 동공 격자를 확인해 값을 바꿔야 한다.

```python
import json
from pathlib import Path

from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8770") as client:
    spec = client.lens_geometric_pupil_density_template()["specification"]
    spec["lens_revision_id"] = "6c48e967-74b4-5901-8186-ba6fafe4797b"  # P0014
    spec["field_index"] = 0
    spec["wavelength_nm"] = 550.0
    spec["higher_pupil_samples"] = [81, 97, 113]
    spec["target_frequency_cycles_per_mm_row_col"] = [
        [100.0, 250.0, 500.0, 800.0],
        [100.0, 250.0, 500.0, 800.0],
    ]
    queued = client.submit_lens_geometric_pupil_density(spec)
    finished = client.wait_lens_geometric_pupil_density(queued["id"])
    if finished["status"] != "succeeded":
        raise RuntimeError(finished.get("error") or finished["status"])

    diagnostic = finished["result"]["summary"]["diagnostic"]
    print(diagnostic["status"])
    print(diagnostic["pinned_source_pupil_density_adequacy_status"])
    exported_job = client.get(
        f"lens-geometric-pupil-density/{queued['id']}/export"
    )
    Path("pupil-density-job-export.json").write_text(
        json.dumps(exported_job, indent=2, ensure_ascii=False) + "\n"
    )
    client.download_artifact(
        finished["result"]["artifacts"][0],
        "geometric-pupil-density-study.json",
    )
```

HTTP API를 직접 쓰는 경우에도 template의 `specification` 객체를 수정해
JSON 본문으로 제출한다. 경로는 순서대로
`GET /api/v1/lens-geometric-pupil-density/template`,
`POST /api/v1/lens-geometric-pupil-density`,
`GET /api/v1/jobs/{id}?expected_kind=lens_geometric_pupil_density` 또는
`GET /api/v1/lens-geometric-pupil-density/{id}`, 마지막으로
`GET /api/v1/lens-geometric-pupil-density/{id}/export`다. `/export`는 검증된
Job JSON을 반환하며, 결과의 content-addressed artifact URL에서는 별도
진단 JSON 원본을 내려받을 수 있다. SDK `wait`가 시간 초과되면 완료로
간주하지 말고 보존한 Job ID로 다시 조회한다.

[P0014 Worker 결과](../reports/camera_platform/frequency-target-20260929/p0014-pupil-density-worker-run-summary.json)는
field index 0(저장 field height 0 mm), 550 nm, 행·열 각각
100·250·500·800 cycles/mm에서 원본 65 → 81 → 97 → 113 동공 표본을
비교했다. 81 단계는 복소 OTF 변화 약 0.01197로 선언한 0.01 허용값을
넘었고, 뒤의 97·113 단계는 연속 통과했다. 저장 원본 histogram 재현을
확인한 뒤 결과 상태는
`geometric_pupil_density_stable_at_declared_targets_model_only`였다.
이는 소스 working tree의 격리 TestClient와 실제 Worker 자식 프로세스에서
완료된 **v1 한 모델·한 node** 증거다. v1의 상태만으로 저장 원본 동공
밀도의 적합성을 판정하지 않는다. 다른 field·파장·주파수나 렌즈로도
일반화할 수 없다.
[설치한 wheel에서의 독립 Worker 재실행](../reports/camera_platform/frequency-target-20260929/p0014-pupil-density-installed-worker-proof.json)도
같은 진단 artifact SHA-256을 반환했다. 이는 패키징과 작업 경로의 재현성 검사이며
실물 렌즈 정확도 검사는 아니다.

v2에서는 **고밀도 기준의 수렴**과 **저장 원본 동공 밀도의 적합성**을 별도
필드로 보고한다. 첫 번째 원본→고밀도 변화는 고밀도 수렴의 연속 통과 횟수에
넣지 않는다. 고밀도 두 단계가 연속 통과해야 최종 고밀도 광선 교점 OTF를
원본 격자와 직접 비교하고 `pinned_source_pupil_density_adequacy_status` 및
`pinned_source_pupil_density_adequate_at_declared_targets`를 채운다. 고밀도
수렴이 미결이면 원본 적합성도 미평가다.

실제 [P0014 최외곽 13.66 mm·550 nm 반례](../reports/camera_platform/frequency-target-20260929/p0014-edge-550-pupil-density-129-installed-worker-summary.json)에서는
65→81→97→113→129 표본 중 마지막 두 고밀도 변화가 0.00668·0.00984로
0.01 안에 들었다. 그러나 원본 65와 최종 129의 복소 OTF 최대 차이는
0.09662다. 기존 v1 상태명만 보면 고밀도 안정성을 저장 PSF의 충분한 광선
밀도로 오인할 수 있었다. 이 보고서는 v2 이전 설치본의 계산 증거이며,
v2에서는 고밀도 기준 안정·원본 동공 밀도 부적합을 분리해 표시한다.
[v2 설치본 중앙 노드](../reports/camera_platform/frequency-target-20260929/p0014-central-550-pupil-density-v2-installed-worker-proof.json)는
원본→안정된 고밀도 기준 차이 0.00274로 선언한 0.01 기준을 만족했고,
[v2 설치본 최외곽 노드](../reports/camera_platform/frequency-target-20260929/p0014-edge-550-pupil-density-v2-installed-worker-proof.json)는
0.09662로 부적합이었다. 두 Job 모두 독립 Worker에서 성공해 결과 JSON
artifact 해시를 확인했다. 이 판정은 해당 기하 모델의 선택 node·이산 주파수에
한정된다.

센서의 열 native Nyquist 약 409.84 cycles/mm 바로 아래인 **400 cycles/mm**까지
따로 제한한 [최외곽 v2 실행](../reports/camera_platform/frequency-target-20260929/p0014-edge-550-pupil-density-v2-to-400-145-installed-worker-proof.json)에서도
97→113 변화는 0.01을 조금 넘어 미통과였고, 113→129·129→145는 연속
통과했다. 안정된 145 광선 기준에 대한 저장 원본 65의 최대 복소 OTF 차이는
0.01582로 0.01 허용값을 넘었다. 따라서 앞선 800 cycles/mm의 부적합이
Nyquist 초과 점 하나 때문만은 아니다. 이 값은 선택한 **기하 렌즈 모델의
원본 동공 광선 밀도**에 관한 것이며, 센서 RAW의 400 cycles/mm 전달 또는
실물 MTF를 측정한 값은 아니다.

복소 OTF는 histogram을 확대해 구한 값이 아니라 새로 추적한 광선 교점에서
직접 계산한다. 그래도 회절·픽셀 개구·센서 각도 응답·다파장/전 시야·실측
렌즈 정확도는 포함하지 않는다. 저장 기하 PSF의 ray 밀도에 대한 수치
안정성은 Scene/RAW 수렴, 카메라 또는 제품 MTF 검증, 제품 순위를 뜻하지
않는다. 원본 ray 밀도가 적합하더라도 저장 histogram bin 간격·양자화와
실행 시 PSF 보간은 따로 검증해야 한다. 결과의
`physical_mtf_comparison_allowed`, `product_ranking_allowed`,
`physical_accuracy_qualified`는 모두 `false`다.

## 저장 Lens에서 고밀도 기하 PSF 후보 만들기

`lens_dense_geometric_psf_candidate`는 위의 **한 node 진단**과 다른 opt-in
Job이다. 저장 Lens revision의 field×wavelength **모든 node**에서 원본
광선 교점과 PSF를 먼저 재현한 뒤, 지정한 더 촘촘한 동공 광선 격자로
기하 ray histogram 후보를 만든다. 원본과 **같은 물리 PSF pitch·support**를
유지하며, Lens DB revision과 기존 PSF asset을 교체하지 않는다. 제출 시
Lens DB·처방·PSF 해시를 고정하고 Worker가 재검사한다. 후보의 NPZ와
경로를 제거한 manifest는 별도의 content-addressed Run artifact로 저장된다.

요청 schema는 `camerae2e_pinned_dense_geometric_psf_candidate_job_v1`이다.
`GET /api/v1/lens-dense-geometric-psf-candidates/template`의 `specification`을
수정해 `POST /api/v1/lens-dense-geometric-psf-candidates`로 제출한다. SDK에는
`lens_dense_geometric_psf_candidate_template()`,
`submit_lens_dense_geometric_psf_candidate()`,
`wait_lens_dense_geometric_psf_candidate()`,
`lens_dense_geometric_psf_candidate()`가 있다. 전용 목록·취소·export와
공통 Job 조회도 제공한다. 아래 실행 증거는 격리 상태의 소스 Worker에서
얻은 것으로, 기존에 실행 중인 `127.0.0.1:8770` 서버에 새 route가 이미
반영됐다는 뜻은 아니다. 해당 서버의 template endpoint를 먼저 확인한다.

| 설정 | 계약 |
| --- | --- |
| `lens_revision_id` | reviewed prescription과 저장 raytrace PSF가 있는 기존 Lens DB revision UUID. 로컬 경로나 사용자가 만든 배열은 받지 않는다. |
| `pupil_samples` | 원본보다 큰 66~257 정수. 이는 후보 광선 격자이지 저장 histogram pitch의 자동 변경이 아니다. |
| `target_frequency_cycles_per_mm_row_col` | 행·열별 증가하는 이산 주파수 점. node별 원본 대비 복소 OTF 차이를 기록하지만 이 값만으로 새 후보의 수렴을 인증하지 않는다. |
| `max_total_requested_pupil_rays`, `max_output_psf_bytes`, `soft_timeout_s` | 광선·배열·시간의 명시적 실행 정책. 기본값은 각각 400,000 rays, 128 MiB, 300초이며 Worker 기본 제한은 420초다. 하드웨어 한계가 아니다. |

[P0014 전체 node 후보 생성 증거](../reports/camera_platform/frequency-target-20260929/p0014-all-node-dense-geometric-psf-candidate-proof.json)는
원본 65→후보 145 동공 표본에서 5 field×3 wavelength의 15개 원본 node를
먼저 재현했다. 후보는 512×512×5×3, 원본과 같은 약 0.612885 µm PSF
histogram pitch이며, 모든 고밀도 node의 광선이 선언된 support 안에 남았다.
후보 archive SHA-256은
`18991743823d2485d5739433090008a56604ab36074df05e1983b5d6dbb1fff4`다.
선택한 400 cycles/mm, field index 3·450 nm·행 방향에서 원본과 후보의
**복소 OTF 절대 차이** 최대값은 0.082815였다. 이는 상대 오차 8.28%나
카메라 MTF 손실 8.28%를 뜻하지 않는다. 원본 대비 차이를 보여줄 뿐,
145 표본 후보가 더 높은 밀도에서 수렴했는지는 전 node에 대해 검사하지
않았다. 같은 histogram pitch의 bin 오차, 회절과 픽셀 개구도 남는다.

## 동일 Scene 격자에서 후보 PSF의 Lens×Sensor 영향 보기

`lens_sensor_dense_psf_sensitivity`는 성공한 후보 Run의 **ID와 archive SHA-256**을
명시적으로 선택하고, 등록된 Lens×Sensor×active mode·연속 에지 Scene·FOV·ROI·
노출을 고정한 채 원본과 후보를 각각 한 번 계산한다. 후보의 **기하 PSF 함수만**
교체하고 원본 Lens의 광량·입사 방향 및 동일 센서 픽셀 연산은 유지한다.
픽셀 개구가 들어가지 않은 기하 PSF이므로 센서 적분을 두 번 적용하지 않는다.
제출·실행 시 후보 Run 소유권, archive·manifest 해시, 원본 Lens revision·PSF·
처방, Sensor와 active mode의 결합을 다시 확인한다. 후보를 DB 기본 응답으로
승격하지 않는다.

요청 schema는 `camerae2e_lens_sensor_dense_psf_fixed_grid_sensitivity_job_v1`이다.
`GET /api/v1/lens-sensor-dense-psf-sensitivity/template`,
`POST /api/v1/lens-sensor-dense-psf-sensitivity`, 전용 조회·목록·취소·export를
사용할 수 있다. SDK도 같은 이름의 `*_template()`, `submit_*()`, `wait_*()`,
조회 함수를 제공한다. 아래 예시는 검증된 등록 ROI 요청 사전
`registered_roi`와 Lens revision UUID를 준비한 뒤의 순서다.

```python
candidate_spec = client.lens_dense_geometric_psf_candidate_template()["specification"]
candidate_spec["lens_revision_id"] = registered_roi["lens_revision_id"]
candidate_run = client.submit_lens_dense_geometric_psf_candidate(candidate_spec)
candidate = client.wait_lens_dense_geometric_psf_candidate(candidate_run["id"])
if candidate["status"] != "succeeded":
    raise RuntimeError(candidate.get("error") or candidate["status"])

candidate_sha = candidate["result"]["summary"]["candidate_archive_artifact"]["sha256"]
spec = client.lens_sensor_dense_psf_sensitivity_template()["specification"]
spec["registered_roi"] = registered_roi
spec["candidate_run_id"] = candidate_run["id"]
spec["candidate_asset_sha256"] = candidate_sha
spec["fixed_grid_goal"]["optical_tile_estimated_working_budget_bytes"] = 2 * 1024**3
comparison_run = client.submit_lens_sensor_dense_psf_sensitivity(spec)
comparison = client.wait_lens_sensor_dense_psf_sensitivity(comparison_run["id"])
if comparison["status"] != "succeeded":
    raise RuntimeError(comparison.get("error") or comparison["status"])
result = comparison["result"]["summary"]["comparison"]
print(result["status"], result["mean_electron_full_scale_normalized_max_delta"])
```

`fixed_grid_goal`은 0.25 native pixel의 에지 이동, 지정한 Nyquist 미만
주파수 점, 256 MiB~8 GiB 타일 추정 정책을 받는다. **이 Job은 refinement를
하지 않는다.** `scene_sampling_converged=false`와
`pupil_density_converged_all_nodes=false`가 유지된다. 8×8 Bayer ROI의
`frequency_curve_status_by_phase`는 `inconclusive_mosaic_channel_contract`다.
보고하는 변화량은 두 위상의 관측 최대 광전자 신호를 분모로 한 최대 절대
광전자 차이와 에지 이동 응답 차이이며, 어두운 픽셀별 상대 오차나 Nyquist
MTF 오차가 아니다. 에지 신호가 양쪽에서 유효하지 않으면 비교도 미결론이다.

[중앙 1°·2016² Scene Worker 비교](../reports/camera_platform/frequency-target-20260929/p0014-dense-psf-fixed-grid-2016-proof.json)는
같은 후보 Run `run-429fd858d7dc42e996aa50440c9f620f`를 사용했다.
원본·후보의 8×8 ROI 광전자 해시는 서로 다르며 입력 Scene·센서는 같았다.
전체 신호 기준 최대 평균 광전자 차이는 0.0743%, 에지 이동 응답 차이는
0.0537%였다. [42° FOV 주변부 8×8 ROI Worker 비교](../reports/camera_platform/frequency-target-20260929/p0014-outer-roi-dense-psf-fixed-grid-proof.json)는
4096×44000 **가상** Scene의 필요한 타일만 처리했고, 원본·후보 에지 신호가
모두 유효했다. 같은 정규화 차이는 각각 0.0860%, 0.0410%였다. 주변부
작업의 자식 프로세스 최대 관측 RSS는 약 518 MB였지만 3 GiB 감시 기준은
하드웨어 경계가 아니다. 이 실행은 44000열 전체 프레임 RAW 계산도,
그 FOV 전체의 수렴 판정도 아니다.

선택 node에서 큰 복소 OTF 차이와 위 8×8 RAW 전 광전자에서 작은 차이는
서로 모순이 아니다. 시야·파장 혼합, 에지와 이산 Bayer 표본, 전체 신호
정규화, 측정 대역이 다르다. 이 사례만으로 400 cycles/mm의 제품 MTF나
PSF 선택의 무해함을 결론 내릴 수 없다. 다음 검증은 같은 물리 Scene·FOV를
유지해 Scene/OI 격자를 연속 정련하고, 후보 동공 광선 밀도와 histogram
pitch도 **독립적으로** 정련한 뒤 주파수별·위치별 응답이 안정되는지 보는
것이다. 현재 두 Job의 `physical_mtf_comparison_allowed`,
`product_ranking_allowed`, `physical_accuracy_qualified`는 모두 `false`다.

## Scene 격자와 기존 고밀도 PSF 후보를 함께 정련

`lens_sensor_joint_scene_dense_psf_ladder`는 위 v1 후보 Run과 등록된
Lens×Sensor×active mode를 묶어, **같은 FOV·Scene·ROI**에서 원본과 후보
광학 모델을 각 Scene 격자마다 계산한다. 요청 schema는
`camerae2e_lens_sensor_joint_scene_dense_psf_ladder_job_v1`이고,
`GET /api/v1/lens-sensor-joint-sampling-ladder/template`에서 계약을 받는다.
`candidate_run_id`와 그 Run의 `candidate_asset_sha256`을 함께 지정한다.
`joint_goal.scene_grid_ladder_row_col`에는 양 축이 정확히 2배씩 증가하는
3~5개 격자를 명시한다. 큰 가상 Scene을 작은 ROI 타일로 다루는
`extended_sparse_roi_128k_v1`은 네 격자에만 명시적으로 선택할 수 있다.
Worker의 기본 제한은 각각 1,800초와 3,600초이며 장비의 메모리 한계가 아니다.

각 단계에는 두 광학 모델의 잡음 전 평균 광전자·에지 이동 응답과 해시,
같은 격자에서의 원본 대비 후보 차이가 기록된다. 두 모델 **각각**에서
연속 두 번의 2배 격자 비교가 선언 허용값 이내일 때만 해당 Scene/실행
PSF 격자의 평균·위상 안정성을 기록한다. 후보는 원본과 같은 저장 PSF
histogram 간격을 사용하므로, 이 결과는 원천 PSF의 bin 간격 또는 동공
표본 수렴을 인증하지 않는다. 8×8 Bayer ROI의 Nyquist MTF도 계속 미결론이다.
전용 제출·조회·목록·취소·export와 SDK의
`lens_sensor_joint_sampling_ladder_template()`,
`submit_lens_sensor_joint_sampling_ladder()`,
`wait_lens_sensor_joint_sampling_ladder()`를 사용할 수 있다.

[P0014 주변부 결합 Worker 결과](../reports/camera_platform/frequency-target-20260929/p0014-outer-roi-joint-scene-psf-ladder-proof.json)는
30° FOV, 8×8 Bayer ROI, 원본 65 동공 표본 PSF와 같은 histogram pitch의
145 표본 후보를 2048×16000→4096×32000→8192×64000→16384×128000
**가상 Scene 격자**에서 비교했다. 두 모델 모두 마지막 두 격자 비교가
평균 광전자 1%, 에지 이동 응답 2% 허용차 안에 연속으로 들었다.
가장 촘촘한 격자에서 원본 대비 후보의 전체 신호 기준 최대 평균 광전자
차이는 0.00105047, 위상 응답 차이는 0.00069873이었다. 이 값은 작은
ROI의 잡음 전 수치 민감도이며, 해당 동공 표본의 전 node 수렴이나
400 cycles/mm 시스템 MTF는 뜻하지 않는다. `frequency_target_points_status`는
`inconclusive_mosaic_channel_contract`이고 자식 프로세스 관측 최고 RSS는
약 1.37 GB였다. 전체 128000열 Scene이나 RAW를 물리 배열로 만들지 않았다.

## 반 간격 기하 PSF 후보의 독립 진단

`lens_dense_geometric_psf_candidate_v2`는 v1 후보와 별도 자산·Job이다.
요청 schema는 `camerae2e_pinned_dense_geometric_psf_candidate_job_v2`이며
`GET /api/v1/lens-dense-geometric-psf-candidates-v2/template`에서 계약을
받는다. 원본 PSF의 `sample_spacing_mm`를 확인하고
`target_histogram_pitch_um`에 **정확히 절반**을 µm 단위로 지정한다.
예컨대 원본 간격 0.6128849172857022 µm라면 목표는
0.3064424586428511 µm다. 원본 512×512 sample-center support는
1023×1023 표본으로 보존한다.

모든 저장 field×wavelength node의 원본 광선·PSF를 먼저 재현한다.
그다음 지정한 동공 격자로 추적한 **같은 광선 교점**을 원본·절반 간격
histogram에 각각 투영하고, 직접 광선 복소 OTF와 두 histogram OTF를
선언한 이산 주파수에서 비교한다. `maximum_histogram_complex_otf_abs_delta`
기본값 0.01은 복소 OTF의 절대 차이이며 상대 MTF 오차가 아니다. 초과 시
후보 생성 자체는 완료되지만 `histogram_target_status`는
`inconclusive_target_histogram_gap`으로 남는다. 결과 NPZ에는 후보 PSF와
재검산 가능한 광선 교점이 저장되며, Run 소유권·해시를 검증한 뒤 읽는다.
SDK는 `lens_dense_geometric_psf_candidate_v2_template()`,
`submit_lens_dense_geometric_psf_candidate_v2()`,
`wait_lens_dense_geometric_psf_candidate_v2()`를 제공한다.

이 후보는 현재 **원본 렌즈의 카메라 convolution 경로에 자동 적용되지
않는다**. v1 후보 overlay는 PSF의 shape와 pitch가 원본과 같아야 하므로
v2를 거기에 넣으면 계약 오류다. 반 간격 후보를 CameraE2E의 위치별
PSF 리샘플링·회전·radiometry와 함께 적용해 동일 Scene에서 RAW까지
비교하는 adapter는 별도 검증이 필요하다. 또한 v2도 고밀도 동공 표본
자체의 수렴, 회절·픽셀 각도 응답·실물 화질을 입증하지 않는다.

[P0014 반 간격 후보의 실제 Worker 결과](../reports/camera_platform/frequency-target-20260929/p0014-fine-histogram-candidate-v2-proof.json)는
161 동공 표본과 15개 저장 node를 사용했다. 저장 pitch 약 0.612885 µm에서
직접 광선 대비 histogram 복소 OTF의 최대 절대 차이는 0.052088이었고,
같은 광선을 0.306442 µm에 투영하면 0.014204로 줄었다. 하지만 선언한
0.01 허용값보다 커 `inconclusive_target_histogram_gap`이다. 두 값은
400 cycles/mm 이하의 선언된 행·열 이산 주파수에서 찾은 최댓값이며,
상대 MTF 백분율이 아니다. 자식 프로세스의 관측 최고 RSS는 약 641 MB였고
2 GiB 감시 기준은 하드웨어 한계가 아니다. 다음 단계는 더 세밀한 histogram
격자의 비용·지원 범위를 명시하고 같은 광선으로 재검산하는 것이다.

[동일 광선의 4배 격자 독립 진단](../reports/camera_platform/frequency-target-20260929/p0014-same-ray-quarter-pitch-psf-diagnostic.json)은
검증된 후보 archive에 보존한 15개 node의 광선 교점만 다시 사용했다.
0.153221 µm 간격, 2045² 표본에서 선언 주파수의 최대 복소 OTF 절대 차이는
0.003433으로 0.01 안에 들었다. 이 JSON은 전 node 재추적이나 새 렌즈
revision이 아닌 **동일 광선의 재 binning 진단**이다. 이후 아래의 별도
Job으로 4배 PSF 배열을 영속 자산으로 저장했다. 기존 strict 경로의 PSF
회전·선형 재표본화는 별도 전달 오차를 만들 수 있으므로, 이 bin 진단만으로
카메라 해상력 합격을 선언할 수 없다.

## 4배 세밀 기하 PSF의 영속 Job

`lens_dense_geometric_psf_q4_rebin`은 검증된 v2 반 간격 후보 Run의
**ID와 archive SHA-256**을 부모로 고정한다. Lens revision UUID와 원본
PSF SHA-256도 제출 시 지정하며, 실행·로딩 때 부모 소유권, Lens 출처,
archive·manifest·배열 해시를 재검사한다. 부모가 저장한 *같은* 고밀도
광선 교점만 사용해 원본의 1/4 pitch에 histogram을 만들며 광선을 다시
추적하거나 Lens DB revision을 바꾸지 않는다. 원본 512² 표본 중심 지지는
2045²로 옮겨 보존한다. 출력 PSF는 픽셀 개구 적용 전의 기하 ray histogram이다.

요청 schema는 `camerae2e_pinned_dense_geometric_psf_q4_rebin_job_v1`이다.
`GET /api/v1/lens-dense-geometric-psf-q4-rebins/template`에서 명시적 설정을
받고, 같은 경로에 `POST`로 제출한다. 전용 조회·목록·취소·export와 SDK의
`lens_dense_geometric_psf_q4_rebin_template()`,
`submit_lens_dense_geometric_psf_q4_rebin()`,
`wait_lens_dense_geometric_psf_q4_rebin()`을 사용할 수 있다. 출력 PSF
256 MiB, 추정 작업 배열 512 MiB, soft timeout 300초의 기본 상한은
실행 정책이며 하드웨어 한계가 아니다. 이 Job은 q4 PSF를 생성·검증하지만
카메라 convolution에 자동 장착하지 않는다.

[P0014 q4 Worker 증거](../reports/camera_platform/frequency-target-20260929/p0014-q4-rebin-job-proof.json)와
[실행 요약](../reports/camera_platform/frequency-target-20260929/p0014-q4-rebin-job-summary.json)에는
부모 `run-577a91741f2c4219b256fe4a12923eb2`에서 파생한
`run-189854f438404df5b15f6a17f5ed8d50`의 15개 node, 2045×2045×5×3
PSF와 약 0.153221 µm pitch가 기록돼 있다. 보존 archive SHA-256은
`90acafb9b1db817034ab5e7496204d3242eee8ee2ab6ac66702fce1f2c1d5d07`다.
같은 광선의 직접 광선 복소 OTF 대비 histogram 최대 절대 차이는 q2에서
0.014204, q4에서 0.003433이었다. q4는 선언된 **이산 주파수**의 0.01
허용값 안에 든 모델 수치 결과다. 관측 최고 자식 RSS 약 1.10 GB와
3 GiB 감시 기준은 해당 실행의 기록이지 가능한 전체 Scene 크기의
하드웨어 경계가 아니다. 동공 광선 밀도, 회절, 픽셀 개구·각도 응답,
제품 MTF 또는 물리 정확도를 인증하지 않는다. 아래의 q4 고정 격자 Job에서만
검증된 자산을 명시적으로 카메라 계산에 적용한다.

## 실제 OI 커널로 재표본화할 때의 간격 확인

`lens_executed_kernel_otf_diagnostic`은 저장된 한 field×wavelength node의
기하 PSF를 CameraE2E strict 경로의 **각도 0°** 표본 중심 좌표·선형
보간·DC 정규화·float32 커널 저장 방식으로 지정 OI 격자에 옮긴다.
그 커널의 복소 OTF를 같은 광선 교점에서 직접 계산한 복소 OTF와 행·열
100, 250, 400 cycles/mm에서 비교한다. 이 진단은 Scene·convolution·
Sensor·RAW·MTF 평가기를 실행하지 않으며, 연속 주파수 대역의 상한도
검증하지 않는다. 아래 수치는 동일 161 동공 표본 광선을 각각 원본·q2·q4
pitch에 binning한 비교다. 여기서 `source`는 **원본 Lens의 65 표본 광선
PSF가 아니라** 동일 161 표본 광선을 원본 pitch에 담은 비교용 histogram이다.

[P0014 OI 격자별 반례](../reports/camera_platform/frequency-target-20260929/p0014-executed-psf-kernel-same-ray-diagnostic.json)에서
OI 간격이 1.178255 µm일 때 field 0·650 nm node의 최대 복소 OTF 절대
차이는 source 0.059442, q2 0.121332, q4 **0.322881**이었다. 세밀한
PSF를 거친 OI 격자에 strict 선형 보간으로 옮기면 오히려 더 나빠질 수
있다. 따라서 PSF pitch만 줄여서 목표 정확도에 도달한다고 판단해서는 안 된다.

[가장 촘촘한 OI 격자의 저장 node 15개 비교](../reports/camera_platform/frequency-target-20260929/p0014-executed-psf-kernel-all-nodes-finest-oi.json)는
OI 간격 0.147281899 µm, 각도 0°에서 최대 차이가 source 0.094627,
q2 0.027462, q4 **0.00692041**이었다. q4의 15개 node 모두 선언된
100·250·400 cycles/mm 점에서 0.01 안에 들었다. 이는 **해당 OI 격자와
저장 node·각도 0°**에 한정한 모델 수치 검사다. 주변부의 field 보간·
방위각 회전, 다파장 광량·CRA 결합, 센서 적분, RAW와 시스템 MTF는 여기서
검증하지 않았다. 실제 Lens×Sensor 결과를 판단하려면 해당 조건을 유지한
카메라 실행과 별도의 결과 수렴 검사가 이어져야 한다.

[P0014의 16개 주파수점/축 추가 검사](../reports/camera_platform/frequency-target-20260929/p0014-dense-sampled-frequency-q4-proof.json)는
같은 0.147281899 µm OI 간격에서 25~375 cycles/mm의 25 간격 점과
409 cycles/mm를 행·열 각각 평가했다. 저장된 15개 field×wavelength
node의 각도 0° 실행 커널과 동일 광선의 직접 복소 OTF 사이 최대 절대
차이는 source 0.093775, q2 0.027476, q4 **0.006957**이었다. q4 자산은
Run 소유권과 15개 PSF plane의 비트 단위 일치도 확인됐다. q4의 15개
node가 선언된 16개 점/축에서 0.01 이내였다는 **모델 수치 증거**이며,
점 사이 연속 주파수 대역의 오차 상한, 시야 보간·회전 또는 Scene·Sensor
통과 후 MTF의 증거는 아니다.

## q4 PSF를 한 Scene 격자에 적용하는 선택형 Lens×Sensor 비교

`lens_sensor_q4_psf_sensitivity`는 영속 q4 후보를 원본 strict raytrace
Lens에 **명시적으로** 적용해, 같은 등록 Sensor·active mode·FOV·에지
Scene·ROI에서 원본 PSF와 q4 PSF를 각각 CameraE2E 광학 계산에 넣는다.
`lens_sensor_q4_psf_overlay`는 q4의 기하 PSF 배열과 물리 sample spacing만
교체하고 원본 Lens의 광량·기하·입사 방향 필드는 유지한다. q4 Run을 읽을
때 소유권·archive 해시·Lens revision·부모 Run 출처를 검사하며, overlay도
원본 PSF 배열·pitch·field·wavelength와 q4 배열의 크기·pitch·에너지·
해시를 대조한다. 이 경로는 회절 PSF나 픽셀 적분 PSF를 받지 않는다.

요청 schema는 `camerae2e_lens_sensor_q4_psf_fixed_grid_sensitivity_job_v1`이다.
`GET /api/v1/lens-sensor-q4-psf-sensitivity/template`에서 현재 계약을 받고,
같은 경로에 `POST`로 제출한다. 전용 조회·목록·취소·export와 공통
`/api/v1/jobs/{id}` 조회를 지원한다. SDK에는
`lens_sensor_q4_psf_sensitivity_template()`,
`submit_lens_sensor_q4_psf_sensitivity()`,
`wait_lens_sensor_q4_psf_sensitivity()`가 있다. `candidate_run_id`와 해당
Run의 `candidate_asset_sha256`을 함께 고정한다. `fixed_grid_goal`에는 정확히
한 개의 `scene_shape_row_col`, `coordinate_policy`, native pixel 기준
`edge_shift_native_pixel_fraction`, 오름차순 Nyquist 미만 평가 점
`target_fractions_native_nyquist`, 타일 작업 추정 예산을 지정한다.
`capture.noise_flag=0`으로 잡음 전 평균 광전자를 비교한다.

```python
spec = client.lens_sensor_q4_psf_sensitivity_template()["specification"]
roi = spec["registered_roi"]
roi["lens_revision_id"] = lens_revision_id
roi["sensor_revision"] = {"identifier": sensor_report_code, "run_id": sensor_run_id}
roi["active_mode_declaration_id"] = active_mode_id
roi["sensor_roi_bounds_row_col_exclusive"] = [1020, 1028, 13920, 13928]
roi["analytic_scene"].update(
    shape_row_col=[16384, 128000], horizontal_fov_deg=30.0,
    edge_offset_deg=14.047293808135992,
)
roi["capture"]["noise_flag"] = 0
roi["accept_approximate_lens_radiometry"] = True
roi["acknowledge_unverified_mode_and_uniform_sensor_response"] = True
spec["candidate_run_id"] = q4_run_id
spec["candidate_asset_sha256"] = q4_archive_sha256
spec["fixed_grid_goal"].update(
    scene_shape_row_col=[16384, 128000],
    coordinate_policy="extended_sparse_roi_128k_v1",
    optical_tile_estimated_working_budget_bytes=8 * 1024**3,
)
job = client.submit_lens_sensor_q4_psf_sensitivity(spec)
result = client.wait_lens_sensor_q4_psf_sensitivity(job["id"])
comparison = result["result"]["summary"]["comparison"]
```

이 예의 16384×128000은 **가상 Scene 좌표 격자**다. Worker는 등록된
작은 ROI에 필요한 광학 타일만 생성하며, 전체 크기의 Scene·RAW 배열을
메모리에 만들지 않는다. 이 크기와 8 GiB 타일 예산은 명시적 실행 정책이지
필요 정확도나 하드웨어 한계의 보편값이 아니다. 실제 실행 가능성과 peak
RSS는 제출 후 Worker 기록으로 확인해야 한다.

결과에는 같은 물리 입력에서의 두 위상 평균 광전자 배열 해시, 전체 관측
최대 신호로 정규화한 최대 평균 광전자 차이와 에지 이동 응답 차이,
각 실행의 OI pitch와 출처를 남긴다. 어두운 픽셀별 상대 오차는 측정하지
않는다. **원본→q4 비교에서는 동공 광선 밀도와 histogram pitch가 동시에
변한다.** 따라서 차이가 작거나 커도 어느 한 축의 효과라고 분리할 수 없다.
한 Scene 격자에서 두 PSF를 비교하는 Job이므로 q4의 Scene/OI 격자 수렴,
Bayer ROI의 Nyquist MTF, 전 화각·제품 수준의 물리 정확도는 입증하지 못한다.
실제 [P0014 Worker 증거](../reports/camera_platform/frequency-target-20260929/p0014-source-vs-q4-camera-fixed-grid-proof.json)와
[고정 격자 결과](../reports/camera_platform/frequency-target-20260929/p0014-source-vs-q4-camera-fixed-grid-summary.json)는
Run `run-f6206b41963c4fcb9e7d55c75578259c`의 성공 및 결과 artifact
SHA-256 검증을 기록한다. 16384×128000 가상 Scene, 0.147282 µm OI 간격,
등록 8×8 Sensor ROI에서 원본 대비 q4의 최대 평균 광전자 차이는 전체
관측 신호 기준 **0.520876%**, 에지 이동 응답 차이는 **0.287875%**였다.
Worker는 약 545초, 관측 최고 child RSS는 약 3.81 GB였다. 두 내부
capture의 `inconclusive_refinement_limit`는 이 Job이 의도적으로 한 격자만
계산했기 때문이며, 두 입력이 같다는 조건 아래 **고정 격자 차이** 자체는
수치 결과로 보존됐다. 이 수치는 q4의 Scene 수렴, histogram pitch 단독
효과, Nyquist MTF 또는 제품 수준 정확도의 합격 판정이 아니다.
