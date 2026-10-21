# 렌즈 × 센서 결합 작업: `0.16.2.dev8` 개발 계약

현재 개발판의 통합 작업과 설치형 검증은
[dev11 안내](camera-platform-joint-workflows-dev11.md)를 사용하세요. 아래는 dev8 당시
계약을 보존한 기록입니다.

기존 Lens DB의 저장 기하 PSF와 Sensor DB의 저장 `db_estimate_v1` 모델을 같은
revision 쌍으로 고정해 RAW, 정적 HDR, 이상적 PWM/LFM을 계산한다. 완료한 단일
RAW는 ISP v2와 기존 데이터셋 형식의 입력이 된다. 이 경로는 개발판이며
`0.16.1rc2` 배포 기능이나 실물 카메라 성능 인증이 아니다.

| 작업 | 원본과 계산 | 현재 경계 |
| --- | --- | --- |
| `lens-sensor-raw` | 선택 렌즈 PSF × 저장 센서 분광 응답·readout으로 원본 정수 RAW를 저장 | 중앙 영상 또는 선택 시야의 국소 PSF 패치. 렌즈 투과율·상대 조도는 미상으로 unity 가정; CRA·회절·PSF 픽셀 적분 정확도 미검증 |
| `lens-sensor-hdr` | **같은 렌즈·센서·정적 장면**의 두 노출을 별도 취득해 원본 `int32` RAW 둘을 보존하고 기존 RAW merger로 합성 | 실제 2×2 RGGB CFA만. 합성기의 scalar 계약 때문에 저장 FPN을 0으로 변경한 시험. 시간 이동·deghosting·제품 HDR mode 아님 |
| `lens-sensor-lfm` | 같은 렌즈·센서의 정적 D65 광전자 응답에 이상적 PWM을 행별 해석 적분하고 두 노출 × 네 위상의 정수 RAW를 새로 취득 | 저장 FPN **가정값**으로 가상 창의 맵을 생성한다. 20 µs/line은 작성한 조건이며 실제 timing·AE·flicker 완화 측정이 아님. HDR과 FPN 조건이 다름 |
| `lens-sensor-isp-replays` | 완료한 `lens-sensor-raw` Job의 **정확히 같은 정수 RAW**를 기존 ISP v2로 재처리해 기본·후보 RGB와 원본 RAW를 저장 | 저장 DB의 native 2×2 Bayer/CFA·ADC·black level을 검증. Quad Bayer·mono는 거부. WB·CCM·tone·bounded sharpen 지원; 별도 적용성 검증 보정이 없는 LSC·denoise는 거부 |
| `retained-raw-datasets` | 완료한 Lens RAW를 값·dtype·seed·출처가 같은 NPZ/ZIP으로 한 번 내보냄 | 현재 한 프레임, unlabeled. split은 해당 ZIP의 표기이고 다른 export와의 holdout 보장은 없음 |
| `lens-sensor-perception` | 이미 만든 Lens RAW 데이터셋 ZIP을 재사용하고 휴대형 RAW index와 출처 보고서만 추가 | Registry 안에서 같은 RAW의 중복 export는 거부. 독립 annotation·정답·실행 모델이 없어 detector 실행 및 metric은 `unavailable` |

단일 정적 RAW를 LFM RAW처럼 다시 해석하지 않는다. LFM에는 새로운 시간 조건의
취득이 필요하다. HDR의 두 RAW 역시 단일 RAW의 처리 변형이 아니라 같은 조합의
서로 다른 노출이다. ISP·데이터셋은 이미 취득한 단일 RAW를 정확히 재사용한다.
HDR fusion과 PWM/LFM은 현재 서로 별도 작업이다. PWM을 넣은 HDR fusion이나
실제 제품의 LFM 제어 동작을 검증했다고 해석해서는 안 된다. 두 계산의 8×8
센서 크기는 작성한 가상 시험 창이다. 저장 native 배열에서 이 창의 물리적 위치와
CFA 위상은 확인되지 않았고, 결과에 저장 배열 크기와 미상인 crop 위치를 분리해
기록한다. LFM의 균일 장면은 렌즈 PSF를 지나지만 공간 선예도·왜곡·주변부 광량의
렌즈 순위를 제공하지 않는다.

SDK에서는 각 `*_template()`로 설치된 입력 계약을 받아 ID와 시험 조건을 채우고,
`submit_*()` 후 `wait(id, expected_kind=...)`로 공통 lifecycle을 조회한다.
결합 예시는 다음과 같다.

```python
template = client.lens_sensor_raw_template()
spec = template["specification"]
spec["lens_revision_id"] = lens_revision_id
spec["sensor_revision"] = {"identifier": report_code, "run_id": saved_run_id}
spec["accept_approximate_lens_radiometry"] = True
raw_job = client.submit_lens_sensor_raw(spec)
raw_result = client.wait(raw_job["id"], expected_kind="lens_sensor_raw")

isp_spec = client.lens_sensor_isp_replay_template()["specification"]
isp_spec["source_lens_sensor_job_id"] = raw_result["id"]
isp_spec["acknowledge_simulation_limits"] = True
isp_job = client.submit_lens_sensor_isp_replay(isp_spec)
isp_result = client.wait(isp_job["id"], expected_kind="lens_sensor_isp_replay")
```

HDR와 LFM은 별도 `lens_sensor_hdr_template()` 및
`lens_sensor_lfm_template()`에서 같은 렌즈 revision·센서 run을 선택한다.
HDR는 실제 native 2×2 RGGB revision을 요구하며, 이전 예시의
`DEP-2209-801` 4×4 Quad Bayer는 제출 전에 거부된다. 실제 지원되는 2×2
`DEF-2512-801` 저장 run으로 개발 계산을 확인했다. 이 센서 선택은 제품 추천이
아니며 저장 전기 readout 값의 출처·가정을 결과에서 확인해야 한다.

각 Job은 제출 시 선택한 Lens·Sensor revision, upstream asset와 실행 코드
fingerprint를 고정하고 Worker에서 다시 확인한다. 전문 GET은 저장 artifact의
해시와 원본 RAW 연결을 재검사하며 export는 완료된 Job에만 허용한다. 개발 트리에서 HDR·LFM 실제 Worker
테스트 4개, ISP·인지 handoff 관련 테스트 12개 및 Lens RAW→ISP 실제 Worker
테스트가 통과했다. 별도 설치형 wheel 검증 결과는
`reports/camera_platform/integrated-workflows-20260928/installed-proof-dev8.json`에
기록한다. 해당 파일이 없거나 여섯 작업이 성공하지 않았다면 설치형 검증은
완료된 것으로 간주하지 않는다.

남은 제품 관문은 명확하다. 첫 8×8 full-field 렌즈 광학 계산이 약 50초여서
대표 해상도에 대한 성능·메모리 검증 이전에는 대화형 사용을 주장할 수 없다.
검증된 렌즈 투과율·시야별 상대 조도·CRA·회절/샘플링과 matched sensor angular
response가 필요하다. native FPN을 유지한 결합 HDR fusion, Quad Bayer의 정당한
HDR/ISP, 렌즈·센서·촬영 조건에 결속된 독립 LSC/잡음 보정, 실제 검출기 가중치와
독립 정답/분할, 다중 장면 데이터셋이 남아 있다. 새 Job은 아직 Composer의
정식 Assembly ID로 통합되지 않았고 화면 폼도 dev8 결합 경로에는 없다.
계산 완료·교차 도구 비교·실측 정확도·제품 승인은 결과에서 계속 분리한다.
