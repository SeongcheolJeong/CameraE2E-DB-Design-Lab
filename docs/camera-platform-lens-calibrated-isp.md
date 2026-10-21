# 렌즈 × 센서 원본 RAW의 선언 보정 ISP: `0.16.2.dev9`

`lens-sensor-calibrated-isp` Job은 완료된 `lens-sensor-raw`의 정확한 `int32`
원본 RAW에 기존 ISP v2의 native 4사이트 LSC와 same-site RAW 잡음 제거를
적용한다. 기본 ISP와 후보 ISP의 RGB·단계 배열을 별도로 저장하며 RAW를 다시
촬영하거나 변경하지 않는다. Job·API·SDK는 `camera_platform`의 공통 조회·대기·
취소 계약을 사용한다.

기존 `saved-sensor-calibrated-isp` 보정 파일은 이 경로에 사용할 수 없다. 그 파일은
다른 장면·RAW·광학 조건에 결속돼 있다. 새 `camerae2e_lens_sensor_isp_calibration_v1`
JSON에는 별도의 숫자 보정 출처와 대상 Lens RAW의 Job ID, 원본 파일/내용 해시,
렌즈·센서 revision, PSF 선택, 가정한 렌즈 광량/상대 조도, native CFA, readout,
가상 mosaic 좌표, 장면·노출·seed를 정확히 기록한다. 제출 전에 적용성을 검사하고
불일치하면 Job을 만들지 않는다. 파일 바이트의 해시와 적용 조건은 검증하지만,
공급자가 선언한 보정 데이터셋의 독립성이나 물리적 정확도는 검증하지 않는다.

```python
template = client.lens_sensor_calibrated_isp_template(
    source_lens_sensor_job_id=raw_job_id
)
binding = template["expected_calibration_binding"]
# 별도 데이터에서 구한 native 4사이트 LSC/noise 숫자와 provenance를
# binding에 맞춘 JSON으로 작성해 calibration_path에 저장한다.
imported = client.import_lens_sensor_isp_calibration(calibration_path)
spec = template["specification"]
spec["source_lens_sensor_job_id"] = raw_job_id
spec["calibration_sha256"] = imported["calibration_sha256"]
spec["acknowledge_simulation_limits"] = True
spec["profile"]["calibration_id"] = "sha256:" + imported["calibration_sha256"]
spec["profile"]["spatial_lsc"]["mode"] = "native_four_site_grid_v1"
spec["profile"]["raw_denoise"].update(
    mode="calibrated_same_site_filter_v1", strength=0.8
)
job = client.submit_lens_sensor_calibrated_isp(spec)
result = client.wait(job["id"], expected_kind="lens_sensor_calibrated_isp")
```

이 경로는 실제 저장 native **2×2 RGGB**와 선형 readout 계약을 요구한다. Quad
Bayer·mono와 보정 적용성이 불명확한 RAW는 거부한다. 렌즈 transmission과
relative illumination은 원본 촬영에서 미상이라 unity로 가정했다. 그 위에서
LSC·잡음 제거 값이 영상을 얼마나 바꾸는지를 볼 수 있지만, 실제 렌즈 shading
교정이나 영상 품질 향상·색 정확도·인지 성능을 입증하지 않는다. 가상 8×8 창은
제품 die의 위치가 확인된 crop이 아니며, 저장 센서 readout/FPN 중 상당수는
실측값이 아닌 가정이다.

개발 트리의 순수 adapter 테스트 11개와 Job/API/SDK·실제 DB 테스트 2개가
통과했다. 별도 설치형 wheel의 독립 Worker에서도 원본 RAW 동일성, 보정 파일
해시, LSC·잡음 제거 단계 적용을 확인했다. 실행 ID와 산출물 해시는
[설치형 검증 기록](../reports/camera_platform/integrated-workflows-20260928/lens-calibrated-isp-installed-dev9.json)에
있다. 이때 사용한 보정 숫자는 별도 작성한 시뮬레이션 fixture이지 실측이 아니다.
제품 수용에는 독립 보정 근거, 렌즈 광량/시야/CRA, 대표 해상도 성능, 실제
정답을 가진 화질·인지 평가가 더 필요하다.
