# Lens × Sensor 통합 작업 · `0.16.2.dev11` 개발판

이 경로는 기존 Lens DB의 저장 기하 PSF와 Sensor DB의 저장 모델 revision을 고정해
계산합니다. `0.16.1rc2` 인도본과는 별개인 개발판입니다. 계산 완료, 설치형 실행,
실측 정확도, 제품 승인은 서로 다른 상태입니다. 실측 정확도와 제품 승인은 아직
확립되지 않았습니다.

| 작업 | 현재 가능한 일 | 범위 |
| --- | --- | --- |
| Lens × Sensor RAW | 선택한 Lens PSF와 Sensor 분광·readout으로 정수 RAW를 취득·보존 | 8–32짝수 크기 가상 창. 렌즈 절대 투과율·RI·CRA와 native crop 위치는 미상입니다. 저장 센서 readout은 출처와 가정을 확인해야 합니다. |
| 정적 HDR | 같은 조합을 두 노출로 각각 취득해 원본 RAW와 fusion을 보존 | 실제 2×2 RGGB만. 현재 merger 계약에 맞추기 위해 FPN을 0으로 지정한 별도 시험이며 움직임·제품 HDR mode는 다루지 않습니다. |
| PWM/LFM 취득 | 같은 조합에 작성한 직사각형 PWM을 행별 적분해 2노출 × 4위상의 정수 RAW를 보존 | 정적 D65, 작성한 20 µs/line, 저장 FPN *파라미터*에서 생성한 가상 창 map입니다. 실물 센서 timing·LFM 성능이 아닙니다. |
| PWM RAW HDR 복원 | **저장된 PWM 원본 RAW**를 새 취득 없이 재생해 행별 유효 노출을 역산하고 두 노출 융합·한 노출 fallback·무효를 별도 mask로 기록 | 저장 FPN map과 읽기 계약을 검증합니다. 개발 예시의 256픽셀 중 융합 4, fallback 112, 무효 140으로 결과는 `inconclusive`입니다. |
| 동일 RAW ISP | 원본 정수 RAW에서 WB·CCM·tone·bounded sharpen 후보를 재처리 | native 2×2 RGGB. 별도 보정 Job은 출처를 선언한 수치 LSC·same-site denoise도 적용하지만, 독립 실측 보정이나 범용 HW ISP는 아닙니다. |
| ISP 영상의 인지 평가 | 완성된 ISP RGB의 shape·dtype·해시와 외부 annotation·prediction·model manifest를 결속해 한 프레임 box matching을 계산 | 모델 추론과 학습, 독립 정답의 진위 확인, dataset mAP는 수행하지 않습니다. 검사 가능한 출처 선언이 필요합니다. |
| RAW cohort | 완료한 Lens × Sensor RAW Job 2–64개를 원본 `int32` 그대로 unlabeled ZIP으로 묶고 train/validation/test를 기록 | 동일 RAW의 split 간 재사용은 거부하고 같은 split 안에서는 하나로 묶습니다. 다른 export 전체에 대한 holdout 독립성은 주장하지 않습니다. |

`lens-sensor-perception`은 RAW 한 건을 데이터셋 입력으로 넘기는 기존 작업이고,
`lens-sensor-isp-perception`은 외부에서 공급한 prediction을 **완료된 ISP RGB**와
대조하는 새 작업입니다. 두 작업 모두 detector를 실행하지 않습니다. cohort는
여러 RAW를 내보내며 라벨을 자동 생성하지 않습니다. 각 단계의 원본과 결과 artifact는
개별 SHA-256으로 고정합니다.

SDK는 각 작업의 `*_template()`에서 요청 형식을 받아 채우고, `submit_*()` 뒤
`wait(id, expected_kind=...)`로 공통 lifecycle을 조회합니다. 아래 코드는 이미
완료된 Job ID를 연결하는 부분입니다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8770") as client:
    raw_spec = client.lens_sensor_raw_template()["specification"]
    raw_spec["lens_revision_id"] = lens_revision_id
    raw_spec["sensor_revision"] = {"identifier": report_code, "run_id": saved_run_id}
    raw_spec["accept_approximate_lens_radiometry"] = True
    raw = client.wait(
        client.submit_lens_sensor_raw(raw_spec)["id"],
        expected_kind="lens_sensor_raw",
    )

    isp_spec = client.lens_sensor_isp_replay_template()["specification"]
    isp_spec["source_lens_sensor_job_id"] = raw["id"]
    isp_spec["acknowledge_simulation_limits"] = True
    isp = client.wait(
        client.submit_lens_sensor_isp_replay(isp_spec)["id"],
        expected_kind="lens_sensor_isp_replay",
    )

    cohort_spec = client.lens_sensor_raw_cohort_template()["specification"]
    cohort_spec["source_jobs"] = [
        {"source_job_id": raw["id"], "split": "train"},
        {"source_job_id": other_completed_raw_job_id, "split": "test"},
    ]
    cohort_spec["acknowledge_simulation_limits"] = True
    cohort = client.wait(
        client.submit_lens_sensor_raw_cohort(cohort_spec)["id"],
        expected_kind="lens_sensor_raw_cohort",
    )
```

HDR와 PWM/LFM은 단일 RAW의 ISP 변형이 아니라 새 노출 계산입니다.
`lens_sensor_hdr_template()`와 `lens_sensor_lfm_template()`에서 같은 Lens revision과
Sensor run을 선택합니다. 저장 LFM이 완료되면
`lens_sensor_pwm_hdr_template()`의 `source_lfm_job_id`에 그 Job ID를 지정합니다.
인지 평가는 `lens_sensor_isp_perception_template()`에서 RGB binding을 받고,
annotation·prediction·model manifest를 각각 import한 뒤 해시를 제출합니다.
요청 필드와 거부 조건은 설치본의 `/docs`에서 확인할 수 있습니다.

개발판 통합 화면은 `/integration-workbench.html`입니다. Lens × Sensor RAW,
정적 HDR, PWM/LFM 취득, ISP, cohort의 실제 API를 사용합니다. 인지 평가는
검증 가능한 외부 문서가 없으면 화면에서 실행하지 않고 API 문서로 안내합니다.
화면의 정적 DOM/JS 계약과 설치본의 HTML·JS·CSS HTTP 200 응답을 확인했습니다.
실제 브라우저에서의 폼 제출은 별도 확인이 필요합니다.

[설치형 dev11 근거](../reports/camera_platform/integrated-workflows-20260928/installed-proof-dev11.json)는
wheel 파일과 설치된 파일의 일치, 복사한 dev9 보존 상태의 원본 SHA, 실제 child Worker
Job, RAW 보존과 결과 artifact를 기록합니다. 2×2 Lens × Sensor 원본 RAW 비교는
[dev9 근거](../reports/camera_platform/integrated-workflows-20260928/lens-sensor-2x2-installed-dev9-v2.json)에
별도로 있습니다. dev11 설치형 검증은 새 광학 취득을 다시 수행한 증거가 아니라,
저장된 dev9 원본을 새 통합 Job이 정확히 소비했다는 증거입니다.

대표 해상도와 다양한 장면에서의 메모리·속도, 렌즈 광량·시야별 PSF 및 센서 각도
응답의 정확한 대응, 제품별 HDR/LFM timing, 독립 ISP 보정, 실제 detector와 라벨,
전역 데이터셋 split 관리는 남아 있습니다. 예를 들어 일부 저장 Lens PSF는 8×8
가상 창에서도 중앙 PSF stack의 추정 메모리가 개발 예산 3 GiB를 넘어
제출 전에 `resource_blocked`로 거부합니다. 이는 제한을 드러내는 사전 검사이지
대표 해상도 성능의 해결이 아닙니다.
