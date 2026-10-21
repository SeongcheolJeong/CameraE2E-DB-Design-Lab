# Lens × Sensor 결합 작업 · `0.16.2.dev14` 개발 계약

이 문서는 `0.16.2.dev14` 개발 계약과 별도 설치본의 한정된 실행 근거를 설명합니다. 설치 검증 기록은 [아래](#설치본-검증)에, 이전 설치본의 분할 PSF 근거는 [dev13 기록](camera-platform-joint-workflows-dev13.md)에 있습니다.

dev14에서는 완료된 Lens×Sensor RAW 네 건을 재촬영 없이 검사하는 2×2 화질 진단, 중앙 strict PSF 분할 실행을 쓰는 HDR·PWM/LFM 요청, 그리고 원본 RAW 좌표에 사용자가 제공한 box를 묶는 데이터셋 경로를 연결합니다. 작업실은 `/integration-workbench.html`입니다. 각 작업은 `POST`로 제출한 뒤 `/api/v1/jobs/{id}` 또는 전용 상세 경로에서 상태와 근거를 다시 읽습니다.

| 작업 | Template / 제출 API | SDK | 작업 종류 |
| --- | --- | --- | --- |
| 저장 RAW 2×2 화질 진단 | `GET /api/v1/lens-sensor-quality-studies/template` / `POST /api/v1/lens-sensor-quality-studies` | `lens_sensor_quality_template()`, `submit_lens_sensor_quality(spec)` | `lens_sensor_quality_study` |
| 중앙 bounded 정적 HDR | `GET /api/v1/lens-sensor-hdr/template?scope=central_bounded` / `POST /api/v1/lens-sensor-hdr` | `lens_sensor_hdr_template(scope="central_bounded")`, `submit_lens_sensor_hdr(spec)` | `lens_sensor_hdr` |
| 중앙 bounded PWM/LFM | `GET /api/v1/lens-sensor-lfm/template?scope=central_bounded` / `POST /api/v1/lens-sensor-lfm` | `lens_sensor_lfm_template(scope="central_bounded")`, `submit_lens_sensor_lfm(spec)` | `lens_sensor_lfm` |
| 사용자 라벨 RAW 데이터셋 | `GET /api/v1/retained-raw-datasets/template?source_kind=lens_sensor_raw_labeled&source_job_id={id}` → `POST /api/v1/retained-raw-datasets/annotations` → `POST /api/v1/retained-raw-datasets` | `retained_raw_dataset_template(...)`, `import_retained_raw_annotation(document)`, `submit_retained_raw_dataset(spec)` | `retained_raw_dataset` |

완료 작업의 전용 상세 API는 각 제출 경로 뒤에 `/{id}`를 붙입니다. 2×2·HDR·LFM의 `/{id}/export`는 JSON을, 단일 RAW 데이터셋의 `/{id}/export`는 ZIP을 반환합니다. 상세 응답에 붙은 artifact URL에서 별도 NPZ/JSON 원본 근거를 받을 수 있습니다.

## 저장 RAW 네 건의 2×2 화질 진단

먼저 `slanted_edge` 장면의 중앙 Lens×Sensor RAW 작업 네 건을 완료합니다. `L1S1`, `L1S2`, `L2S1`, `L2S2`는 각각 렌즈 1·2와 센서 1·2의 교차 조합이어야 합니다. 각 source job의 저장 결과·정수 RAW artifact·Lens/Sensor revision·원본 해시를 다시 검증합니다. 네 건 모두 같은 ISP v2 profile로 처리하며, 새 광학 계산이나 RAW 촬영은 하지 않습니다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8770") as client:
    template = client.lens_sensor_quality_template()
    # template["source_jobs"]에서 완료된 RAW job ID와 실행 FOV를 확인합니다.
    spec = template["specification"]
    spec["source_jobs"] = {
        "L1S1": lens_1_sensor_1_raw_job_id,
        "L1S2": lens_1_sensor_2_raw_job_id,
        "L2S1": lens_2_sensor_1_raw_job_id,
        "L2S2": lens_2_sensor_2_raw_job_id,
    }
    spec["acknowledge_simulation_limits"] = True
    queued = client.submit_lens_sensor_quality(spec)
    finished = client.wait(
        queued["id"], expected_kind="lens_sensor_quality_study"
    )
    summary = finished["result"]["summary"]
    print(summary["comparison"]["reasons"])
    print(summary["cases"]["L1S1"]["edge_mtf50"])
```

`cases`에는 원본 RAW 해시·개별 조건·포화율·유효 ISP 픽셀 비율과, 추정 가능할 때만 단일 프레임 선형 ISP edge MTF50(`cycles/output_pixel`)가 들어갑니다. 작은 8×8 창, 대비 부족, invalid ROI 또는 포화는 `edge_mtf50.available=false`와 이유로 남습니다. 각 source에 독립 seed의 반복 프레임이 하나뿐이므로 `temporal_snr_db.available=false`입니다. RAW의 공간 편차를 시간 잡음으로 바꾸지 않습니다.

`comparison`은 장면·노출·seed·창 크기·ISP/CFA·PSF 실행 방식·upstream asset과 실행 화각을 확인합니다. 네 case 모두 조건이 맞고 MTF50도 가용할 때만 `edge_mtf50_diagnostic_delta_allowed=true`이며, 이때 인접한 렌즈 단독 또는 센서 단독 변경의 모델 기반 **진단 차이**를 기록합니다. 화각이 다른 경우 공통 angular sampling과 ROI 비교 계약이 성립하지 않아 수치 차이를 차단합니다. `product_ranking_allowed`는 항상 `false`입니다. `cases`의 개별 수치만 보고 제품 우열을 정하지 마세요.

작업실의 **저장 Lens × Sensor 2×2 비교**에서 네 완료 작업을 선택할 수 있습니다. 결과 화면은 각 case의 MTF50 또는 불가 사유, 화각 비교 상태, temporal SNR 불가 및 제품 순위 제한을 보여 줍니다.

## 중앙 bounded HDR와 PWM/LFM

두 template 모두 `scope="central_bounded"`를 명시해야 v2 계약을 반환합니다. 기존 `scope="central"`은 v1 전체 PSF stack 경로입니다. bounded 모드는 필요한 strict raytrace PSF를 잘라 순차 계산하며 광학 모델이나 검증 수준을 바꾸지 않습니다. 사전 자원 예측은 개발용 휴리스틱으로, 실행 성공·최대 메모리·시간을 보장하지 않습니다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8770") as client:
    hdr = client.lens_sensor_hdr_template(scope="central_bounded")["specification"]
    hdr["lens_revision_id"] = lens_revision_id
    hdr["sensor_revision"] = {"identifier": report_code, "run_id": saved_run_id}
    hdr["exposure_s"] = [0.0005, 0.008]  # 정적 장면의 정확히 두 노출
    hdr["sensor_size"] = [16, 16]         # 각 축 짝수, 8–32
    hdr["accept_approximate_lens_radiometry"] = True
    hdr_job = client.submit_lens_sensor_hdr(hdr)

    lfm = client.lens_sensor_lfm_template(scope="central_bounded")["specification"]
    lfm["lens_revision_id"] = lens_revision_id
    lfm["sensor_revision"] = {"identifier": report_code, "run_id": saved_run_id}
    lfm["sensor_size"] = [16, 16]
    lfm["exposure_s"] = [0.001, 0.004]
    lfm["pwm_period_s"] = 0.002
    lfm["duty_cycle"] = 0.5
    lfm["accept_approximate_lens_radiometry"] = True
    lfm["acknowledge_simulation_limits"] = True
    lfm_job = client.submit_lens_sensor_lfm(lfm)
```

HDR은 고정 장면의 두 RAW 노출을 합성합니다. 저장 센서의 PRNU·DSNU·column FPN은 현재 scalar HDR merger에서 **실행 시 0으로 비활성화**되며, 움직임·deghosting·PWM 또는 제품 HDR 동작은 다루지 않습니다. LFM은 작성한 이상적 PWM 광량과 가정한 20 µs 행 시간을 적분합니다. 공간 FPN은 가상 창에서 모델 생성한 것이며 실제 센서 측정 맵이 아닙니다. 두 작업은 서로 동일한 readout/FPN 조건의 성능 대조가 아닙니다. 화면의 **정적 RAW HDR**와 **PWM/LFM 위상 진단**에서 분할 strict PSF를 선택해 같은 template를 사용할 수 있습니다.

## 사용자 라벨이 포함된 원본 RAW 데이터셋

라벨은 완료된 Lens×Sensor 작업의 **원본 int32 RAW mosaic 픽셀**에 붙입니다. `source_kind=lens_sensor_raw_labeled`는 template 조회 옵션이며, 반환한 제출 spec의 `source_kind`는 `lens_sensor_raw`입니다. 특정 `source_job_id`로 조회해야 서버가 그 RAW의 해시·크기·좌표계가 들어간 `annotation_template`를 반환합니다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8770") as client:
    template = client.retained_raw_dataset_template(
        source_kind="lens_sensor_raw_labeled", source_job_id=raw_job_id
    )
    annotation = template["annotation_template"]
    annotation["provenance"].update(
        provider_id="review-team", annotation_set_id="set-001",
        method="human_review", coverage="full_frame_reviewed",
    )
    annotation["objects"] = [
        {"label": "object", "bbox_xyxy": [1, 1, 5, 5]}
    ]
    imported = client.import_retained_raw_annotation(annotation)

    spec = template["specification"]
    spec["source_job_id"] = raw_job_id
    spec["annotation_artifact_sha256"] = imported["artifact"]["sha256"]
    spec["split"] = "train"
    spec["acknowledge_simulation_limits"] = True
    queued = client.submit_retained_raw_dataset(spec)
```

Box는 `[x1, y1, x2, y2]` 반개구간이며 원점은 RAW 좌상단입니다. ISP RGB 좌표가 아닙니다. 서버는 bound source·RAW 크기·좌표 범위·provenance 형태를 검사하고, 정규화한 JSON을 content hash artifact로 저장합니다. 선택적 `expected_sha256`은 **서버가 정규화한 canonical JSON 바이트**의 해시여야 합니다. 데이터셋은 원본 RAW와 제공 라벨을 보존하지만 라벨의 진실성·독립성·검출기 점수나 cross-export split 독립성을 검증하지 않습니다. 화면의 **단일 RAW 데이터셋 → 렌즈 × 센서 RAW · 사용자 라벨**에서도 같은 순서로 가져오기와 제출을 진행합니다.

## 공통 적용 범위

Lens 절대 투과율·상대 조도는 unity placeholder이고 CRA, 회절, 픽셀 적분 PSF와 실측 정확도는 확립되지 않았습니다. 저장 `db_estimate_v1` 센서와 작은 가상 RAW 창만 결합 계산에 사용하며, 이 창은 실제 native array에서 위치가 특정된 crop이 아닙니다. 2×2 진단은 기존 단일 프레임을 재사용하므로 시간 SNR이나 동등 화각 제품 순위를 만들 수 없습니다. 사용자 라벨은 외부 선언일 뿐 ground truth나 인지 성능 인증이 아닙니다. 이 경로의 성공 상태는 소스·출력 계약과 수치 실행의 근거이며 상용 화질 승인은 아닙니다.

## 설치본 검증

새 상태와 별도 child Worker에서 [dev14 wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev14-py3-none-any.whl)을 설치해 SDK 제출→대기→재조회 및 artifact 다운로드를 확인했습니다. [전체 증거 JSON](../reports/camera_platform/integrated-workflows-20260928/joint-dev14-installed-proof.json)에 작업 ID, 소스·출력 해시, 조건, 자원 예측, 결과가 있습니다. Wheel SHA-256은 `aa4ab205ffc54ab592e4ff7928f7c1247dc63ffdadc54c6e0f485f7af0b6564e`이며 설치 파일 1,022개의 바이트를 wheel과 대조했습니다. 완료 상태와 blob은 기본 런타임을 건드리지 않는 [별도 보존 상태](../outputs/camera-platform-runtimes/dev14-joint-proof-20260928/registry.sqlite)에 저장했습니다. 복사 상태의 registry audit는 24개 이벤트·18개 artifact를 검증했고, 설치본 UI를 이 상태로 재연결해 저장 결과가 다시 표시됨을 확인했습니다.

- 저장 DB의 렌즈 P0014/P0248 × 센서 `DEF-2512-801`/`DEF-2412-804`로 중앙 bounded `32×32` RAW 4건을 실행했습니다. 원본 int32 RAW 해시는 [소스 직접 계산 증거](../reports/camera_platform/integrated-workflows-20260928/bounded-psf-2x2-source-dev13.json)의 4건과 정확히 일치했고, 각 작업은 전체 PSF stack을 생성하지 않은 `strict_bounded_streaming` 실행 근거를 보존했습니다.
- 네 RAW의 2×2 화질 작업은 같은 ISP profile에서 edge MTF50 개별 진단 4건을 계산하고 원본 RAW 네 개를 NPZ에서 재검증했습니다. 실행 수평 화각이 `0.03819`, `0.04383`, `0.05429`, `0.06230`도여서 공통 각도 표본·ROI 비교가 성립하지 않습니다. 따라서 수치 paired delta와 제품 순위를 차단했습니다. 시간 SNR은 독립 반복 촬영이 없어 불가입니다.
- P0014의 bounded 정적 HDR `8×8` 원본 RAW `2×8×8`과 64개의 유효 fusion 픽셀, P0248의 bounded PWM/LFM `8×8` 원본 RAW `2×4×8×8`을 별도 Worker에서 실행하고 저장 JSON/NPZ를 다시 읽었습니다. 두 작업의 PSF 실행 모드는 `strict_bounded_streaming`이며 자원 수치는 실측 RSS가 아닌 휴리스틱입니다.
- 첫 RAW에 사용자 작성 box 1개의 source-bound annotation을 서버에서 정규화·저장한 뒤 v3 데이터셋 ZIP을 만들었습니다. ZIP 안의 원본 RAW는 소스와 값·dtype이 정확히 같고 annotation artifact SHA도 보존됩니다. 라벨은 `user_provided_unverified`이며 다른 export와의 holdout 독립성은 주장하지 않습니다.

관련 74개 소스 회귀가 통과했고, 마지막 입력 검증 변경 뒤에는 집중 회귀 18개·Ruff·작업실 JavaScript 계약 검사를 재실행했습니다. 설치본에서 작업실 HTML/JS 응답 200과 annotation의 과대 정수 입력 422도 확인했습니다. 별도 포트의 설치본 작업실을 실제 Chrome에서 열어 저장된 2×2·HDR·LFM·라벨 데이터셋 결과와 다운로드 링크를 다시 읽었습니다. 브라우저 폼을 통한 신규 제출과 일반 해상도 성능은 이번 설치형 검증에 포함되지 않았습니다. 위 결과는 소형 가상 창의 계산·저장 연결 검증이며 물리 정확도·제품 성능 순위·상용 준비 완료를 뜻하지 않습니다.
