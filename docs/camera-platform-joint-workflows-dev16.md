# Lens × Sensor 통합 작업 · `0.16.2.dev16` 개발 검증

`dev16`은 기존 Lens DB revision과 Sensor DB run을 결합하는 RAW 작업에 **왜곡을 반영한 픽셀 중심 각도 지도**를 추가하고, 저장된 bounded PWM/LFM RAW를 HDR fusion Job으로 이어지는 경로를 수정했다. 이는 [dev15의 공통 입력 장면, 제한된 ISP·인지·데이터셋 작업](camera-platform-joint-workflows-dev15.md) 위에 쌓은 개발 계약이다. 새 [wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev16-py3-none-any.whl)을 별도 경로에 설치하고 child Worker와 별도 상태 저장소에서 실행한 결과는 [dev16 설치본 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev16-installed-proof.json)에 있다. 이 증거의 `package.wheel_sha256`과 설치 파일 일치 검사가 재현 대상 wheel을 식별한다.

| 이번 설치본에서 확인한 작업 | 확인 결과 | 해석 한계 |
| --- | --- | --- |
| 저장 Lens 2개 × 저장 Sensor 2개의 동일 입력 장면 RAW | 네 조합의 8×8 `int32` RAW, 원본 해시, 450/550/650 nm 왜곡 반영 픽셀 중심 지도 `[파장, 행, 열, 행·열 각도]` 보존 | 중심 위치의 기하 지도이며 유한 픽셀 면적·시야별 PSF를 적분한 공통 출력 격자가 아니다. |
| 2×2 화질 Job | 550 nm 중심 지도에서 네 조합의 겹치는 ROI와 ROI 안 native 픽셀 중심 수 `64/36/16/16`을 기록 | 공통 입력 장면의 각도 표본이 왜곡 반영 native 최소 간격보다 조합별 `3.60/3.14/2.25/1.96`배 성기다. 비교 가능한 MTF 차이와 제품 순위는 계속 차단된다. |
| bounded PWM/LFM → 저장 HDR fusion | 두 노출 × 네 PWM 위상 × 8×8의 저장 원본 RAW를 그대로 재사용하고 SDK 제출·공통 Job 대기·상세 조회를 완료 | 256 픽셀 중 dual fusion 4, 한 bracket fallback 112, invalid 140으로 결과는 `inconclusive`다. 제품 HDR/LFM 성능 증거가 아니다. |

이전에 bounded PWM/LFM 보고서 v2를 HDR 입력 검사에서 거부하던 계약 불일치를 바로잡았다. HDR 결과는 소스 LFM 스키마와 `bounded_streaming` PSF 계산 모드 및 원본 RAW 해시를 기록한다. 설치본 증거에서 LFM Job과 HDR Job의 `original_pwm_raw_dn` 바이트 해시가 같다. 원본 보존과 Job 연결이 확인된 것이며, 재촬영이나 독립 물리 측정이 수행된 것은 아니다.

## 작업실과 SDK 사용

작업실 `/integration-workbench.html`에 `03A` 저장 PWM RAW → HDR 폼이 있다. 완료된 bounded LFM Job을 선택하고 제한 확인을 한 뒤 제출할 수 있다. 공통 `/api/v1/jobs/{id}`로 상태를 기다리고 `/api/v1/lens-sensor-pwm-hdr/{id}`에서 결과와 증거를 읽는다. SDK에서도 같은 순서를 따른다.

```python
template = client.lens_sensor_pwm_hdr_template()
spec = template["specification"]
spec["name"] = "bounded PWM RAW HDR review"
spec["source_lfm_job_id"] = completed_lfm_job_id
spec["acknowledge_simulation_limits"] = True
job = client.submit_lens_sensor_pwm_hdr(spec)
client.wait(job["id"], expected_kind="lens_sensor_pwm_hdr")
result = client.lens_sensor_pwm_hdr(job["id"])
```

작업실의 Lens×Sensor 2×2 결과에는 왜곡 반영 중심 지도의 존재, 550 nm의 기하학적 겹침, 입력 장면 각도 표본 간격과 native 최소 간격의 비율을 표시한다. 입력 장면의 **같은 화각**은 네 RAW의 **같은 출력 각도·공간 주파수 표본**을 뜻하지 않는다. 중심 지도를 얻었다고 이전 화질 차단을 해제하지 않았다. `edge_mtf50_diagnostic_delta_allowed=false`, `matched_native_output_angular_grid=false`, `product_ranking_allowed=false`가 현재 결론이다. 이 조건에서 숫자상 edge MTF 차이를 나열해 후보를 선정하면 잘못된 의사결정이 된다.

## 재사용 범위와 다음 게이트

dev15의 32×32 RAW 사례, 같은 RAW에 적용하는 RGGB Adaptive Laplacian, 원본 보존 데이터셋, 운영자가 가중치 SHA를 신뢰 선언한 로컬 YOLO 호출은 [당시 설치본 증거](camera-platform-joint-workflows-dev15.md)에 남아 있다. 이번 dev16 설치본 증거는 **8×8 결합 RAW·2×2 지오메트리와 bounded LFM→HDR**를 다시 실행한 것이다. 다른 기능을 이번 wheel에서 모두 재실행하거나 정확도 검증했다고 주장하지 않는다. 일반 ISP graph, Quad Bayer remosaic, 대표 크기의 독립 라벨 인지 평가 및 일반 RAW 학습 데이터셋은 아직 통합되지 않았다.

다음 게이트는 (1) 입력 장면의 충분한 각도 표본과 유한 픽셀·PSF 통합 출력 그리드 및 공통 주파수/ROI 지표, (2) 저장 센서의 실제 line/frame timing·노출 순서·readout mode와 시간적으로 변하는 장면/PWM/rolling 결합, (3) CFA·보정 자산의 출처를 보존하는 확장 가능한 ISP 단계, (4) 대표 해상도·독립 라벨·신뢰된 모델 런타임의 인지 및 데이터셋 검증이다. 현재 `low_gain`과 `hcg`는 저장된 **readout operating point**이며 제품 HDR/LFM mode가 아니다. 20 µs 행 시간 역시 작성한 시험 조건이다. 실측 정확도, 제품 성능 순위 및 상용 승인 상태는 `not_established`다.
