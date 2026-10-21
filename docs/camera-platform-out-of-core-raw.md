# 디스크 기반 광학 타일 → 전체 RAW 진단 경로

`camera_platform.lens_sensor_out_of_core_optical_raw`는 기존 CameraE2E의
strict raytrace PSF와 센서 평균 광전자 계산을 전역 좌표의 타일로 실행합니다.
완전하고 중복 없는 센서 타일을 `mean_electrons.npy` 메모리맵에 채운 뒤,
`lens_sensor_out_of_core_readout`이 행 묶음별로 RAW·기준 DN·전압을 별도
`.npy` 메모리맵에 기록합니다. 전체 Scene, optical image, 평균 광전자 또는
RAW 배열을 이 경로가 한꺼번에 RAM에 조립하지 않습니다.

라이브러리 호출과 별도로 `lens_sensor_out_of_core_raw_job`은 **저장 Lens DB
revision × 실행 가능한 저장 Sensor DB run**을 선택하는 typed Platform 작업으로
이 경로를 연결합니다. `/api/v1/lens-sensor-disk-raw`에서 제출·상태 조회·취소·
결과 export를 제공하며, Worker가 자식 프로세스에서 실행합니다. 전체 해상도
ISP를 제공하거나 제품 active mode를 추론하는 기능은 아닙니다.

`lens_sensor_registered_roi_disk_raw_job`은 별도 작업입니다. Registry에
저장한 active mode 선언의 전체 행·열을 **전역 좌표 기준**으로 사용하고,
지정한 오프축 ROI만 평균 광전자·RAW로 계산·저장합니다. 기존 가상 중심
crop 작업의 입력이나 의미는 바뀌지 않습니다. 이 경로도 등록 선언을 실제
제품 제원으로 검증하거나 전체 active area를 촬영한 것으로 간주하지 않습니다.

## 등록 active mode의 오프축 ROI

먼저 `/api/v1/lens-sensor-active-modes`에 선택한 Sensor DB run에 묶인
`camerae2e_fullfield_tile_study_active_mode_v1` 선언을 등록합니다. 새 작업
`/api/v1/lens-sensor-registered-roi-disk-raw`는 그 Registry ID와 저장 Lens
revision/Sensor run, 전역 반열린 ROI `[r0,r1,c0,c1]`, 해석적 Scene, 노출,
타일 정책을 받습니다. caller 경로는 받지 않습니다. SDK에는
`lens_sensor_registered_roi_disk_raw_template()`·
`submit_lens_sensor_registered_roi_disk_raw()`·
`wait_lens_sensor_registered_roi_disk_raw()`가 있습니다.

제출 응답의 `submission_preflight_status=worker_preflight_pending`는 요청의
좌표·자산 결속과 빠른 작업량 검사만 통과했다는 뜻입니다. 이 시점에는 PSF를
로드하거나 광학 타일별 메모리·halo를 검사하지 않습니다. 상세 사전검사는
Worker 자식에서 수행합니다. 성공 결과의 `result.summary.resource_preflight`에
그 근거가 남고, 사전검사에서 차단되면 `result.preflight_failure`의
`cause_status`와 `resource_evidence`로 이유와 관련 추정치를 조회합니다.
제출 중에는 선택한 DB·PSF 원본 해시를 동기 검증하므로, 저장 파일이 큰
경우 SDK의 기본 HTTP 요청 제한 30초 이내 접수를 보증하지는 않습니다.

```python
from camera_platform.sdk import PlatformClient

lens_revision_id = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"
sensor_code = "DEP-2209-801"
sensor_run_id = "74e15e696e8d14827bc641346dc92d113a24997da6ec13efb51d9e2583a4f188"

with PlatformClient() as client:
    option = client.lens_sensor_disk_raw_sensor_option(sensor_code, sensor_run_id)
    if not option["supported_disk_raw_diagnostic"]:
        raise ValueError("선택한 저장 Sensor run은 이 RAW 경로에서 실행 불가")
    saved = option["sensor_revision"]
    # 2048×14000은 이 예시에서 엔지니어가 시험용으로 선언한 전역 좌표입니다.
    # Sensor DB가 증명한 제품 active mode로 해석하지 않습니다.
    mode = {
        "schema": "camerae2e_fullfield_tile_study_active_mode_v1",
        "mode_id": "engineering-2048x14000-zero-origin",
        "sensor_run_id": sensor_run_id,
        "sensor_report_code": saved["report_code"],
        "sensor_result_sha256": saved["result_sha256"],
        "active_rows": 2048,
        "active_cols": 14000,
        "pixel_pitch_um": option["pixel_pitch_um"],
        "sensor_center_offset_from_optical_axis_um_row_col": [0.0, 0.0],
        "sensor_plane": "perpendicular_to_optical_axis",
        "sensor_orientation_deg_clockwise": 0,
        "cfa_origin_row_col": [0, 0],
        "image_plane_binding": {
            "reference_plane": "saved_prescription_image_plane",
            "axial_offset_um": 0.0,
        },
    }
    registered = client.register_lens_sensor_active_mode(
        sensor_code, sensor_run_id, mode
    )
    # 전체 선언 경계의 저장 Lens field 지원 범위는 큰 Scene 계산 없이 먼저 확인합니다.
    fov = client.inspect_registered_lens_sensor_fov(
        lens_revision_id, sensor_code, sensor_run_id, registered["id"]
    )
    print(fov["geometric_fov"]["status"], fov["geometric_fov"].get("reason"))
    spec = client.lens_sensor_registered_roi_disk_raw_template()["specification"]
    spec["lens_revision_id"] = lens_revision_id
    spec["sensor_revision"] = {
        "identifier": sensor_code, "run_id": sensor_run_id,
    }
    spec["active_mode_declaration_id"] = registered["id"]
    spec["sensor_roi_bounds_row_col_exclusive"] = [1000, 1008, 12000, 12008]
    spec["sensor_tile_shape_row_col"] = [4, 8]
    spec["readout_row_chunk_rows"] = 4
    spec["analytic_scene"].update(
        shape_row_col=[4096, 28000], horizontal_fov_deg=42.0,
        edge_offset_deg=14.0,
    )
    spec["accept_approximate_lens_radiometry"] = True
    spec["acknowledge_unverified_mode_and_uniform_sensor_response"] = True
    job = client.submit_lens_sensor_registered_roi_disk_raw(spec)
    result = client.wait_lens_sensor_registered_roi_disk_raw(job["id"])
    if result["status"] != "succeeded":
        raise RuntimeError((result.get("result") or {}).get("preflight_failure")
                           or result.get("error"))
    raw = result["result"]["summary"]["array_artifacts"]["raw_dn"]
    client.download_artifact(raw, "registered-roi-raw_dn.npy")
```

FOV가 필요한 첫 질문이 저장 렌즈의 **기하학적 field 범위와 선언 active
경계의 관계**라면 위의 `inspect_registered_lens_sensor_fov()`까지만 실행해도
됩니다. 그 검사에는 고해상도 Scene이나 RAW가 필요하지 않습니다. 검사
결과가 `within_saved_model_support`여도 선언 자체와 렌즈 장착·화각·광량·
센서 주변부 응답을 검증한 것은 아닙니다. 이 예시는 한 ROI를 시험하므로
전체 active의 촬영 가능 여부를 증명하지도 않습니다.

현재 수치 결합은 `sensor_center_offset_from_optical_axis_um_row_col=[0,0]`,
`sensor_orientation_deg_clockwise=0`, `cfa_origin_row_col=[0,0]`, 저장
prescription image plane의 axial offset `0`만 허용합니다. 전체 선언 경계가
렌즈의 저장 field table 밖이면 full-field 결과는 `outside_saved_model_support`
또는 blocked로 남습니다. **선택 ROI 자체**가 strict 기하·PSF field·Scene halo
검사를 통과하는 경우에만 ROI RAW를 실행할 수 있습니다. full-field/FOV
qualified 필드는 이 경우에도 false입니다.

렌즈의 저장 기하 PSF·알 수 없는 광량 unity placeholder를 유지하고, 저장
`db_estimate_v1` 센서의 spectral QE/CFA/readout을 선언 active 좌표 전체에
동일하게 적용합니다. 이 field-uniform 센서 응답은 명시적 **proxy 가정**이며
주변부 픽셀의 CRA·마이크로렌즈·입사각 응답 검증이 아닙니다. 전체 active 크기의
calibration/FPN/etendue 맵은 현재 작업에서 거부합니다. 광학 평균 타일과
readout의 RNG·생성 FPN·CFA가 모두 센서의 전역 행·열 좌표를 사용하며,
작은 합성 시험에서 전체 계산을 ROI로 자른 값과 대조했습니다. 픽셀 적분의
부동소수점 차이는 정수 RAW에 영향을 주지 않은 시험 범위에서 확인했습니다.

전체 active 선언은 축당 65,536, 총 268,435,456셀까지 좌표 정책상 허용하고,
Scene 가상 격자는 축당 65,536, 총 536,870,912셀까지 입력을 허용합니다.
**이 배열을 통째로 RAM에 만드는 허용량이 아닙니다.** 실제 출력 ROI는 축당
8..128, 최대 16,384셀, optical tile 최대 256개이고 readout 행 묶음은 ROI
높이보다 작아야 합니다. 각 타일의 PSF/역기하/Scene halo, 광학 작업 배열,
readout 임시 배열, 모델 상주 배열, 디스크 여유는 별도로 사전 검사합니다.
전역 좌표가 들어 있는 full-shape Sensor 구성은 저장한 DEP 센서의
2048×14000 선언에서 상주 NumPy 배열 2,208바이트, 보정 맵 없음으로
관측했습니다. 그 모델 구성 전후 프로세스 RSS 차이는 별도 실행에서 약
122 MB였고 저장 512×512×5×3 렌즈 PSF 로딩도 포함합니다. 32×32 구성의
동일 측정은 상주 Sensor 배열 2,208바이트, RSS 차이 약 108 MB였습니다.
이들은 실행 환경 스냅샷이며 peak RSS 보증이나 하드웨어 한계가 아닙니다.

저장 Lens 4daeca8f… × DEP-2209-801 run과 *엔지니어가 시험용으로 선언한*
2048×14000 active 좌표, `[1000,1008,12000,12008]` 오프축 ROI,
4096×28000/42° Scene을 실제 DB + 별도 Worker 자식 프로세스에서 실행해
**8×8 평균 광전자·정수 RAW·기준 DN·전압 파일을 저장**했습니다. Worker
dev45 source checkout에서 Worker 8.544초, 별도 설치 wheel에서 8.691초였고,
두 실행의 정수 RAW SHA-256이 일치했습니다. 제출은 각각 0.034초였으며
이 시간은 현재 선택 자산에서의 관측치입니다. 정수 RAW 범위는 271..351 DN입니다.
전체 선언
경계는 저장 렌즈 field 범위에서 `within_saved_model_support`, reason `null`
이었고, 선택 ROI 광학 타일 2개와 source tile 작업 합계 50이었습니다.
광학 입력 간격은 약 0.678 µm, saved pixel pitch 1.22 µm(약 1.80
sample/pixel)였습니다. 이는 **수치 ROI RAW 실행**의 증거이며 고해상도
입력 수렴·제품 FOV의 증거는 아닙니다. 상태와 각 artifact checksum은
[dev45 source checkout 결과](../reports/camera_platform/mtf-support-20260928/registered-roi-8x8-dev45-source-proof.json)와
[dev45 설치 wheel 결과](../reports/camera_platform/mtf-support-20260928/registered-roi-8x8-dev45-installed-proof.json)에
남겼습니다. 겹치는 source tile은
센서 타일 간 재계산될 수 있으므로 타일 수와 source 작업 합계를 결과에
표시합니다. 상세 검사는 Worker에서 모델·PSF를 한 번 구성하고 광학 타일을
한 번씩 계획한 뒤 실행합니다. 모델에 따른 후보 source tile 합계는 현재
4,096개 정책으로 제한하며, 이것은 실행 시간이나 RSS 상한이 아닙니다.
기본 Worker 제한은 1,800초, SDK 기본 대기는 큐 대기를 포함해 1,860초지만
큰 ROI와 고밀도 Scene의 제한 시간 내 완료는 보증하지 않습니다.

같은 active 선언과 ROI에서 Scene을 8192×56000으로 바꾼 입력도 dev45
설치본에 0.034초에 접수됐지만, Worker의 상세 사전검사에서 광학 계산 전에
차단됐습니다. PSF 재표본 격자는 577×577로 planner 범위 안이고 Scene
halo도 경계 안이었으나, 첫 optical tile의 합산 작업 메모리 **추정치**
587,263,200바이트가 선언 정책 536,870,912바이트(512 MiB)를 넘었습니다.
실패 결과에 추정치·한도·타일 좌표와 `blocked_working_memory_policy`를
구조화해 남겼고 RAW 파일은 없습니다. [dev45 설치본 Worker 차단 기록](../reports/camera_platform/mtf-support-20260928/registered-roi-4x-dev45-installed-policy-proof.json)과
[이전 prepare-only 기록](../reports/camera_platform/mtf-support-20260928/registered-roi-4x-preflight-only.json)을
구분해 보아야 합니다. 512 MiB는 하드웨어 한계나 실제 최대 RSS가 아닌
서비스 정책입니다. 반면 8192×56000을 31파장 float64 Scene 배열 하나로
모두 구체화하면 약 106 GiB가 필요하므로, 전체 배열을 RAM에 만드는 방식은
이번 32 GiB Mac에서 불가능합니다. 현재 ROI 경로는 필요한 영역만 생성해
이 문제를 피합니다. 이 결과만으로 4배 입력의 실행 가능성·RAW 수렴을
주장할 수는 없습니다.

이후 **생산 기본 정책을 바꾸지 않고** 별도 dev45 설치본 진단 프로세스에서
해당 작업의 광학 타일 한도만 1 GiB로 높였습니다. 같은 4배·8×8 ROI가
44.308초에 완료됐고, 자식 프로세스의 `ru_maxrss`는 470,089,728바이트로
관측됐습니다. 따라서 이 고정 사례의 512 MiB 차단은 하드웨어 실패가 아니라
실제 RSS보다 큰 정책 추정으로 인한 것입니다. 2배와 4배의 정수 RAW는
같았지만 평균 광전자는 최대 0.18535 e⁻, 상대 최대 약 0.028% 달랐습니다.
[격리 실행 기록](../reports/camera_platform/mtf-support-20260928/registered-roi-4x-dev45-diagnostic-rusage-proof.json)과
[2배↔4배 배열 비교](../reports/camera_platform/mtf-support-20260928/registered-roi-2x-4x-dev45-diagnostic-comparison.json)에
근거를 남겼습니다. 이는 한 렌즈·센서·ROI의 한 단계 비교입니다. 프로세스
자손과 swap은 실행 환경에서 계측하지 못했고, 4배보다 촘촘한 기준·MTF·
전체 화각·물리 성능은 검증하지 않았습니다. 다른 조합에 대한 생산 정책
상향 근거로 일반화할 수 없습니다.

## 저장 DB 작업 선택과 실제 확인 범위

SDK에서는 `lens_sensor_disk_raw_template(query=...)`로 렌즈·센서 목록과
요청 틀을 조회하고, `lens_sensor_disk_raw_sensor_option(identifier, run_id)`로
저장 센서의 실행 가능 여부와 pixel pitch를 확인합니다. 요청에는 선택한
렌즈 revision UUID와 센서 report code/run hash, **사용자가 선언한 가상 중심
crop**의 크기·pitch·mode ID, 해석적 D65 slanted edge Scene의 크기·FOV,
고정 노출과 타일 크기를 명시합니다. 근사 렌즈 광량과 가상 기하의 한계도
각각 승인해야 합니다. 사용자가 로컬 파일 경로를 전달하는 계약은 없습니다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient() as client:
    template = client.lens_sensor_disk_raw_template(query="DEP-2209-801")
    spec = template["specification"]
    spec["lens_revision_id"] = "4daeca8f-5d81-57b0-8ae3-928f23de2b36"
    spec["sensor_revision"] = {
        "identifier": "DEP-2209-801",
        "run_id": "74e15e696e8d14827bc641346dc92d113a24997da6ec13efb51d9e2583a4f188",
    }
    sensor = client.lens_sensor_disk_raw_sensor_option(**spec["sensor_revision"])
    spec["virtual_sensor_geometry"].update(
        mode_id="virtual-32x32", pixel_pitch_um=sensor["pixel_pitch_um"]
    )
    spec["accept_approximate_lens_radiometry"] = True
    spec["acknowledge_virtual_geometry_and_limits"] = True
    job = client.submit_lens_sensor_disk_raw(spec)
    finished = client.wait_lens_sensor_disk_raw(job["id"])
    raw_artifact = finished["result"]["summary"]["array_artifacts"]["raw_dn"]
    client.download_artifact(raw_artifact, "raw_dn.npy")
```

위 ID는 이 저장소의 실행 증거에 사용한 revision입니다. 다른 DB를 쓰면 목록에서
revision을 선택하고, 센서 옵션의 pitch를 가상 crop에 명시해야 합니다.
성공 결과에는 mean electrons, 정수 RAW, 연속 기준 DN, volts 및 광학 타일
실행 근거의 SHA-256 주소가 있으며, Registry의 파일 기반 저장은 `.npy` 전체를
Python `bytes`로 만들지 않습니다. 기본 SDK 대기는 660초이고, 해당 작업의
Worker 기본 제한은 600초입니다. 대기 시간은 사용자가 늘릴 수 있지만 Worker
제한을 늘리는 것은 별도 실행 설정 변경이 필요합니다.

현재 typed 입장 정책은 가상 센서 최대 256×256, Scene 최대 4096×4096,
센서 타일 최대 256개, 개별 광학 타일 추정 최대 512 MiB, 결합 작업 추정
최대 1 GiB, readout 순간 배열 추정 최대 64 MiB, 출력 파일 총량 최대
32 MiB, 사전 검사 후 디스크 여유 최소 256 MiB입니다. readout 행 묶음은
센서 전체 행보다 작아야 합니다. 이 수치는 서비스가 정한 admission 정책이지
Mac의 물리 RAM·강제 RSS 상한·디스크 예약이 아닙니다. Scene·PSF halo와
광학 작업량 검사를 통과해야 실제 실행됩니다. 완료 파일을 Registry blob에
게시하는 동안 원본과 게시본이 잠시 함께 존재하므로 디스크 사용량은 최종
파일 합계보다 클 수 있습니다.

저장 렌즈와 센서의 **32×32 가상 crop, 1024×1024 Scene, 1.0° Scene FOV**
조합은 실제 DB로 광학 타일 4개와 전체 RAW 파일 저장까지 실행했습니다.
이때 광학 convolution 간격은 약 0.422 µm, 센서 pitch는 1.22 µm였고,
입력 Scene 표본은 센서 픽셀당 약 2.89개였습니다. 이는 계산 실행 근거이며
입력 격자 수렴이나 제품 FOV 확인은 아닙니다. 256×256/4096×4096은
**허용 가능한 입력 정책 상한**일 뿐 그 크기의 실행 완료 증거가 아닙니다.
동일한 저장 32×32 사례에서 광학 group-local 재사용을 명시적으로 선택한
dev44 설치 wheel Worker 실행은 43.069초였고, 재사용 group 2개에서
source tile 180개·sparse convolution 7,056회·kernel 2,844개를 기록했습니다.
평균 광전자·RAW·기준 DN·전압 네 배열의 SHA-256은 이전 legacy 실행과
모두 일치했습니다. 근거는
[dev44 재사용 proof](../reports/camera_platform/mtf-support-20260928/virtual-disk-raw-32x32-reuse-dev44-installed-proof.json)에
있으며, 한 사례의 시간은 큰 ROI의 처리량 보증이 아닙니다.

광학 임시 파일은 `state/out_of_core_work/<job-id>/` 아래에만 만들고,
자식 프로세스가 종료된 것을 Worker가 확인한 뒤 성공·실패·취소·제한 시간
초과 모두에서 **그 작업 ID의 디렉터리만** 제거합니다. symlink 또는 예상 밖
경로는 삭제를 거부합니다. Worker 프로세스 자체가 SIGKILL되거나 호스트가
종료되면 이 후처리가 실행되지 않아 잔여물이 남을 수 있습니다. 그런 경우
동일 경로를 자동 덮어쓰지 않으며, 운영자가 실행 상태와 파일 소유 범위를
확인한 후 처리해야 합니다.

## 입력과 실행 계약

- 입력은 `AnalyticStrictGeometryTileProvider`, 고정 노출의 선형 mosaic
  `digital_raw` CameraE2E `Sensor`, 전체 센서를 정확히 덮는 반열린 타일 목록,
  strict PSF 샘플 각도와 광학 source tile 크기입니다. 타일 하나가 센서
  전체를 차지하면 평균 광전자 배열도 한 번에 RAM에 만들어지므로 거부합니다.
- `OpticalDiskRawResourcePolicy`에 광학 타일 정책,
  `OutOfCoreReadoutPolicy`, 센서 타일 수, 평균 파일·전체 출력 디스크 예산,
  결합 작업 메모리 추정 예산과 디스크 여유량을 선언해야 합니다.
- `preflight_optical_to_disk_raw()`가 **모든** 센서 타일의 Scene halo,
  기하 질의, PSF 격자와 센서 crop 계약을 광학 실행 전에 확인합니다.
  한 타일이라도 실패하면 전체 작업을 거부합니다.
- `compute_optical_to_disk_raw()`는 임시 디렉터리에서 디스크 기반 평균
  배열을 만든 뒤 readout 정책을 다시 검사합니다. 이후 광학 타일을 쓰고,
  완성된 평균을 읽기 전용 메모리맵으로 다시 열어 readout을 실행합니다.
  완료 시 출력 디렉터리를 이동하며, 오류 시 해당 호출이 만든 임시
  디렉터리만 지웁니다. 기존 출력 디렉터리를 덮어쓰지 않습니다.

테스트용 전체 예시는
[`test_lens_sensor_out_of_core_optical_raw.py`](../tests/platform/test_lens_sensor_out_of_core_optical_raw.py)에
있습니다. 핵심 호출 형태는 다음과 같습니다.

```python
plan = preflight_optical_to_disk_raw(
    provider, sensor, output_dir,
    sensor_tiles_row_col_exclusive=tiles,
    source_tile_shape_row_col=(128, 128),
    sample_angles_deg=angles,
    seed=71,
    row_chunk_rows=8,
    resource_policy=policy,
)
if plan["status"] != "ready_under_declared_policy":
    raise ValueError(plan["reason"])

result = compute_optical_to_disk_raw(
    provider, sensor, output_dir,
    sensor_tiles_row_col_exclusive=tiles,
    source_tile_shape_row_col=(128, 128),
    sample_angles_deg=angles,
    seed=71,
    row_chunk_rows=8,
    resource_policy=policy,
)
raw = np.load(result["raw_dn_path"], mmap_mode="r")
```

반환 경로를 나중에 다시 열 때에는 `metadata.json`의 SHA-256과 파일을
대조해야 합니다. 파일 시스템 수준의 변경 불가능성은 이 모듈이 강제하지
않습니다. `tile_execution.jsonl`에는 각 타일의 모델·전역 좌표·광학 및
센서 실행 근거가 한 줄씩 들어갑니다. 전체 광학 타일 사전 검사 결과는
해시와 가장 큰 작업 추정치로 요약되므로, 큰 센서에서 사전 검사 목록을
RAM에 쌓지 않습니다.
`reuse_optical_groups=True`로 인접 타일의 광학 계산을 공유하면 각 자식 JSONL 줄의
`optical_execution` 카운터가 반복됩니다. `optical_reuse.key_sha256`별로 한 번만
합산해야 하며, 최상위 `optical_*_actual` 값은 이미 그렇게 집계됩니다.

## 수치·난수 의미

readout의 물리 연산 순서는 기존 `resample_sensor_capture()`와 같습니다:
평균 광전자에 암전류를 더하고, 선택된 shot noise와 응답 FPN을 적용한 후
full-well을 제한합니다. 그 다음 부호 있는 readout bias, 선택된 read noise,
gain과 ADC를 적용합니다. 원본 평균 광전자, 연속 `reference_dn`, 정수
`raw_dn`, 아날로그 진단 `volts`는 별도로 저장됩니다. 고정 보정 맵이 있으면
그 값을 사용하고, 없으면 선언한 sigma와 fixed-pattern seed에서 생성합니다.

난수 실현은 **기존 NumPy 순차 RNG와 비트 일치하지 않습니다**. shot/read
noise와 생성 FPN의 균일 난수는 seed·절대 행·절대 열·stream ID로 정해지고,
Poisson·정규 분위수로 변환됩니다. 같은 입력에서 행 묶음 크기를 바꿔도
결과는 동일합니다. 기존 경로와 잡음 없는 결과의 정확한 일치, 작은
fixture의 잡음 평균·분산 비교는 테스트했지만, 모든 파라미터·극단
분포·수치 환경에서의 동등성까지 검증한 것은 아닙니다.

## 메모리·디스크 경계와 남은 검증

센서 픽셀이 `N`, 한 readout 묶음이 `T`픽셀, 센서 열이 `C`개일 때 기본
배열 파일의 데이터는 평균 `8N`바이트와 RAW·기준 DN·전압 `20N`바이트,
합계 최소 `28N`바이트입니다. 여기에 `.npy` 헤더, 타일 근거와 메타데이터가
더해집니다. readout 임시 배열의 정책 추정치는 `512T + 256C`바이트이며,
광학 타일·상주 모델 배열 추정과 별도로 비교합니다. 예를 들어 5천만 픽셀은
기본 배열 데이터만 약 1.4 GB의 디스크 공간이 필요합니다.

이 수치는 **정한 입장 정책**이며 하드웨어 메모리 한계 또는 peak RSS 상한이
아닙니다. NumPy·SciPy의 내부 배열, 운영체제 페이지 캐시, 다른 프로세스와
파일 시스템 여유 공간은 실행 중 달라질 수 있습니다. 디스크 여유 검사도
실행 전 스냅샷일 뿐 예약이 아닙니다. 대형 실행에는 별도 자식 프로세스와
외부 RSS·시간 감시가 필요합니다.

현재 경로는 strict 저장 PSF의 수치 halo가 선언된 Scene 안에 있고 각 센서
타일의 광학 의존 범위를 확인할 수 있는 경우에만 작동합니다. 자동 Scene
격자 수렴, 제품의 실제 active mode·mount 결합, 회절·CRA·픽셀 각도 응답의
물리 검증, 전체 해상도 ISP·인지 평가 및 제품 수준 FOV 검증은 아직 이
경로가 제공하지 않습니다. 메모리를 줄였다는 사실로 광학 정확도나
제품 적합성을 주장해서는 안 됩니다.
