# Lens × Sensor 통합 작업 · `0.16.2.dev12` 개발판

이 개발판은 [dev11 통합 작업](camera-platform-joint-workflows-dev11.md)의 RAW·정적 HDR·PWM/LFM·ISP·외부 예측값 인지 평가·데이터셋 기능을 유지합니다. 이번 변경은 무거운 저장 Lens DB PSF를 쓰는 **중앙 raytrace RAW**의 실행 방식을 추가한 것입니다. 광학 모델이나 센서의 물리 정확도를 높인 변경은 아닙니다.

검증한 설치 파일은 [pyisetcam 0.16.2.dev12 wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev12-py3-none-any.whl)입니다.

## 중앙 raytrace PSF 분할 실행

`lens_sensor_raw_template(scope="central_bounded")`는 별도 v3 요청 계약을 반환합니다. 기존 `central` v1, `local_field_psf` v2와 결과를 구분합니다. v3는 시야·각도·파장 PSF 전체 5D stack을 미리 만들지 않고 필요한 커널을 최대 4개 캐시에 계산합니다. 리샘플 후 정확히 0인 외곽을 대칭으로 잘라 FFT에 사용하며, 원본 커널 배열을 캐시에 붙잡지 않도록 복사합니다. 저장 결과에는 실행 모드, 원래 커널 크기, 실제 잘린 최대 크기, 생성 커널 수와 cache peak를 남깁니다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8770") as client:
    spec = client.lens_sensor_raw_template(scope="central_bounded")["specification"]
    spec["lens_revision_id"] = lens_revision_id
    spec["sensor_revision"] = {"identifier": report_code, "run_id": saved_run_id}
    spec["sensor_size"] = [8, 8]
    spec["accept_approximate_lens_radiometry"] = True
    queued = client.submit_lens_sensor_raw(spec)
    finished = client.wait(queued["id"], expected_kind="lens_sensor_raw", timeout=180)
    assert finished["status"] == "succeeded"
```

통합 화면 `/integration-workbench.html`의 **광학 적용 범위 → 중앙 raytrace 분할 계산**도 같은 v3 template와 작업 API를 사용합니다. P0014 같은 무거운 렌즈는 기존 `central` 선택에서 계속 `resource_blocked`입니다. 범위를 명시적으로 바꿔야 v3가 적용됩니다.

## 설치본에서 확인한 것

[설치형 RAW 근거](../reports/camera_platform/integrated-workflows-20260928/installed-bounded-psf-dev12.json)는 wheel과 설치 파일 1020개 일치, 별도 child Worker의 P0014·P0248 8×8 정수 RAW 작업 완료, 저장 artifact 재조회와 수치 해시 비교를 기록합니다. P0014는 약 52초, P0248은 약 20초였습니다. P0248 RAW는 과거 전체 stack 계산 결과와 정확히 같았습니다. P0014는 과거 전체 stack 작업이 120초 안에 완료되지 않아 대응하는 완성 RAW가 없습니다. 따라서 P0014에 대해 알고리즘 간 RAW 일치를 주장하지 않습니다.

[설치형 후속 작업 근거](../reports/camera_platform/integrated-workflows-20260928/installed-bounded-downstream-dev12.json)는 **같은 v3 원본**을 이용해 원본 바이트가 같은 unlabeled dataset, ISP v2 재처리, 제공된 box에 대한 한 프레임 인지 대조, 두 RAW 원본 cohort를 별도 Worker 작업으로 완료한 기록입니다. 인지 작업은 detector를 실행하거나 독립 정답의 진위를 확인하지 않았습니다. HDR와 PWM/LFM의 설치본 범위는 [dev11 근거](../reports/camera_platform/integrated-workflows-20260928/installed-proof-dev11.json)를 참고해야 합니다. dev12에서 이 두 계산을 다시 실행했다는 뜻은 아닙니다.

[dev12 검증 요약](../reports/camera_platform/integrated-workflows-20260928/development-evidence-dev12.json)에 관련 Python 검사 71개, 화면 계약 검사, 설치 파일 검증과 미확립 항목을 모았습니다.

v3 제출 전의 3 GiB 자원 검사는 리샘플 커널과 원본 PSF 배열의 크기로 계산한 **개발용 휴리스틱**입니다. 실행 시간이나 실제 peak RSS의 상한이 아닙니다. P0014에 대한 이전 소스 단독 실행에서는 약 2.2 GB peak RSS를 관측했지만, 이후 캐시 복사와 정책이 바뀌었으므로 그 수치를 최종 설치본의 메모리 측정으로 사용하지 않습니다.

현재 입력은 8–32 크기의 가상 센서 창입니다. Lens 절대 투과율·상대 조도·CRA와 픽셀 적분 PSF 검증은 미확립이며, 저장 `db_estimate_v1` 센서만 이 경로에서 지원합니다. 대표 해상도·다양한 장면·제품별 HDR/LFM timing·독립 ISP 보정·실제 detector 추론·실측 정확도는 아직 검증되지 않았습니다. 따라서 이 증거는 통합 API와 수치 계산이 실행된 근거이지, 상용 제품 성능 승인 근거가 아닙니다.
