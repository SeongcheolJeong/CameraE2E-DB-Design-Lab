# 보유 DB 기반 CameraE2E 통합 작업: 개발판

현재 Lens×Sensor 결합 작업의 실행 범위와 근거 한계는
[0.16.2.dev32 물리 근거·적용 범위](camera-platform-physical-evidence-dev32.md)를 먼저 보세요.
이전 [dev31 문서](camera-platform-joint-workflows-dev31.md)는 해당 시점의
설계·실행 기록으로 남깁니다.
아래 본문은 명시된 dev7 시점의 계약 기록입니다.

이 문서는 기존 `pyisetcam` 계산 자산을 `camera_platform`의 Job·API·SDK·Worker에
연결한 **`0.16.2.dev7` 개발판** 계약을 설명한다. 동결된 `0.16.1rc2` 배포물의 기능이 아니다.
화면은 `/integration-workbench.html`이며, 모든 작업의 전문 API 응답과 원본
artifact는 같은 Registry에 남는다. 계산이 성공했다는 사실은 실측 정확도나
제품 선정 승인을 뜻하지 않는다.

이 문서는 dev7 시점의 범위와 검증 기록이다. 후속 dev8에서 Lens×Sensor 결합
HDR·LFM·동일 RAW ISP·인지용 데이터셋 handoff를 추가한 현재 계약과 별도 wheel
검증은 [결합 작업 문서](camera-platform-joint-workflows.md)를 따른다.

화면 폼은 현재 Lens×Sensor, HDR, LFM, 무보정 ISP 재처리, RAW 데이터셋을 지원한다.
보정 ISP와 외부 제공 예측 평가는 같은 Job lifecycle의 API·SDK로 제출하며, 화면
입력 폼은 아직 제공하지 않는다.

| 작업 | 기존 엔진과 연결한 실제 계산 | 고정된 한계 |
| --- | --- | --- |
| `lens-sensor-raw` | Lens DB의 해시 검증된 v2 ray-histogram PSF 한 revision을 저장 Sensor DB의 `db_estimate_v1` native 분광 응답·readout과 조합해 정수 RAW를 계산한다. 원본 `.npy`를 저장한다. 중앙 시야 전체 계산 또는 저장된 특정 시야 PSF를 국소 패치에 적용하는 v2 경로를 선택한다. | 450/550/650 nm D65, 작은 균일/edge 장면의 **기하학적 미리보기**다. 시야 PSF를 골라도 실제 주변부 영상 위치·CRA·왜곡·상대 조도는 계산하지 않는다. 절대 렌즈 투과율과 상대 조도는 1로 가정한다. 이 가정을 명시적으로 받아들여야 제출된다. |
| `saved-sensor-hdr` | 하나의 저장 센서 run으로 정적 HDR 차트를 두 노출에서 독립 촬영해 정수 RAW 둘과 합성 rate·마스크를 저장한다. `zero_fpn_override` 또는 저장 simulation의 픽셀·열 FPN map을 알고 역보정하는 `model_known_native_fpn`을 선택한다. | 후자는 **모델을 알고 쓰는 보정**이지 별도 dark/flat에서 얻은 독립 실측 보정이 아니다. 정적 독립 노출이고 움직임 보정·PWM/LFM·제품 HDR 모드를 검증하지 않는다. |
| `saved-sensor-lfm` | 저장 센서로 이상적 PWM 광원의 high/low 정적 광전자 응답을 계산하고, 해석적 행별 노출 겹침으로 두 노출·네 위상의 평균 전하를 적분한 다음 기존 readout/FPN으로 정수 RAW를 취득한다. 중간 광량에서 선형성을 확인하고 원본 RAW·타이밍·적분 조건을 보존한다. | 8×8 crop과 20 µs line time은 선언한 시험 조건이지 저장된 제품 timing이 아니다. 동일 seed의 짝지은 민감도 비교이며 일반 시간 solver의 전 위상 재실행, 센서 HDR 모드·실측 flicker 완화·모든 위상 무응답을 증명하지 않는다. |
| `saved-sensor-isp-replays` | 완료된 `saved-sensor-quality` Job의 **동일 정수 RAW 프레임**을 기존 ISP v2의 고정 WB·CCM·tone·sharpening으로 다시 처리한다. 기본 profile과 변경 profile의 RGB 및 유효 마스크를 보존한다. | 지원 입력은 저장 native RGGB와 명시된 선형 ADC 계약이다. 보정 자산이 없는 LSC·denoise는 거부한다. 출력 차이는 화질 개선이나 색 정확도 점수가 아니다. |
| `saved-sensor-calibrated-isp` | 완료된 화질 Job의 정확한 int32 RAW에 별도 JSON 보정값을 적용해 ISP v2의 4사이트 LSC와 같은 사이트 RAW denoise를 실행한다. 기준·후보 RGB, gain/noise 진단 배열, 원본 RAW를 함께 저장한다. 보정 JSON 파일 SHA-256과 소스 RAW·CFA·readout·기하·획득 조건을 묶는다. | 보정 출처와 독립성은 공급자 선언이며 실측으로 검증되지 않았다. 좌표는 시뮬레이션 mosaic의 (0,0)이지 제품 die가 아니다. 한 RAW의 전후 차이를 화질 향상으로 단정할 수 없다. |
| `retained-raw-datasets` | 완료된 `saved-sensor-quality` Job의 첫 원본 RAW 또는 완료된 `lens-sensor-raw` Job의 원본 RAW를 값·dtype·shape·seed가 같은 NPZ/ZIP으로 옮긴다. 기존 dataset validator와 RAW-aware perception indexer를 실행하고 렌즈·센서 revision 및 선택 시야를 보존한다. | 한 장면의 **라벨 없는** 프레임이다. split은 개별 export의 표기일 뿐 같은 RAW가 다른 export에서 train/test로 중복되지 않는다는 보장이 없다. 재촬영·DNG·자동 라벨링·detector 실행·mAP 계산은 하지 않는다. |
| `saved-sensor-perception` | 완료된 화질 Job의 특정 RGB 프레임에 고정한 사용자 annotation·prediction·모델 manifest JSON을 받아 기존 `pyisetcam.task_perception`의 박스 매칭으로 TP/FP/FN·precision/recall/F1을 계산한다. 세 JSON의 canonical SHA-256과 프레임 좌표/원본 해시를 확인한다. | 검출기 실행이나 가중치 업로드는 없다. 공개 API에서 모델 바이트를 등록하지 않으므로 서비스 사용의 모델 해시는 manifest 선언값이다. 정답도 외부 제공이므로 단일 프레임 결과를 mAP·모델 성능·실물 카메라 인지 성능으로 읽으면 안 된다. |

저장 Sensor DB run 선택은 report code와 정확한 run ID를 요구한다. 제출 전에
snapshot과 현재 실행기의 호환성, 기본 자산 해시, CFA·readout·광학 조건을
검사한다. 실행 중 원본이나 실행 코드가 바뀌어도 작업은 실패한다. 공통
`GET /api/v1/jobs/{id}`는 상태를 제공하고, 각 전문 GET은 저장 결과와
artifact 무결성을 확인한다. `PlatformClient.wait(id, expected_kind=...)`는
공통 상태를 기다린 뒤 전문 결과를 읽는다.

`saved-sensor-quality`의 결과 ID를 ISP 재처리·인지 비교·데이터셋 내보내기 입력으로
사용하면 같은 저장 프레임을 기준으로 산출물을 얻을 수 있다. Lens×Sensor RAW도
이제 데이터셋 내보내기의 원본이 된다. 정적 HDR, PWM 진단은 각각 독립적인 모델
실험이다. 이 결과를 자동으로
하나의 카메라 설정이나 후보 순위로 합산하지 않는다. 서로 다른 광학·시간·FPN
계약을 합치려면 조합의 기준면과 보정·타이밍 일치부터 확인해야 한다.

API·SDK에서 각 `/template`을 먼저 읽고, 반환된 `specification`에 자산 ID와
시험 조건, 제한 확인 필드를 채운 뒤 제출한다. 작업 이름과 지표의 단위는 결과
JSON에 기록된다. 결과의 `physical_validation`은 `not_established`다.

SDK에서는 각 `*_template()`로 현재 설치본의 입력 계약과 후보를 읽고,
`submit_*()`으로 제출한 다음 `wait(job["id"], expected_kind=...)`로 같은
Job lifecycle을 기다린다. 전문 GET이 출처와 산출물 해시를 다시 검사한다.
예를 들어 정적 HDR 작업은 다음 순서다.

```python
template = client.saved_sensor_hdr_template()
spec = template["specification"]
spec["sensor_revision"] = {"identifier": report_code, "run_id": saved_run_id}
spec["acknowledge_simulation_limits"] = True
job = client.submit_saved_sensor_hdr(spec)
result = client.wait(job["id"], expected_kind="saved_sensor_hdr")
assert result["status"] == "succeeded", result.get("error")
```

렌즈 시야별 비교에서는 `client.lens_sensor_raw_template(scope="local_field_psf")`에서
v2 입력을 받아 `field_psf_index`와 `field_azimuth_deg`를 지정한다. 완료한 RAW를
데이터셋으로 옮길 때는
`client.retained_raw_dataset_template(source_kind="lens_sensor_raw")`의 v2 입력에
`source_job_id=raw_job["id"]`를 지정한다. 두 작업의 원본 정수 RAW 바이트와
렌즈·센서 revision, 선택한 PSF 시야는 결과와 ZIP manifest에서 다시 확인할 수
있다. 이 export의 `split`은 다른 ZIP과의 중복 검사를 수행하지 않는다.

인지 지표는 먼저
`client.saved_sensor_perception_template(quality_job_id=..., sensor_run_id=..., scene=...)`
에서 `image_binding`을 읽는다. annotation과 prediction JSON 양쪽에 이 값을 그대로
넣고, versioned model manifest와 함께
`client.import_saved_sensor_perception_artifact(kind, document)`로 등록한다.
반환된 세 SHA-256을 작업 입력에 넣어 제출하고
`client.wait(id, expected_kind="saved_sensor_perception")`로 결과를 읽는다.
prediction JSON은 해당 모델 manifest 및 선언한 checkpoint SHA-256을 정확히
가리켜야 한다. 이 절차가 검출기 실행을 뜻하지는 않는다.

공간 LSC·RAW denoise는 `saved_sensor_calibrated_isp_template()`에 완료된 화질 작업
ID와 보고서·장면 index를 넣어 **정확한 보정 binding**을 먼저 받아야 한다. 별도
보정 데이터의 출처·방법·불확실성과 native 4사이트 숫자값을 가진 JSON을 만들고,
`import_saved_sensor_isp_calibration(path)`로 등록한다. 반환 SHA-256을 profile의
`calibration_id="sha256:<값>"`과 작업 입력의 `calibration_sha256`에 넣어 제출한다.
등록은 파일 바이트의 정체성만 확인한다. 공급자가 선언한 보정 독립성이나 실측
정확도는 검증하지 않는다. 결과 조회는 현재 센서 DB가 없어도 동결된 원본·보정·
결과 artifact의 무결성을 검사해 과거 기록을 열 수 있다.

[dev7 설치 검증](../reports/camera_platform/integrated-workflows-20260928/development-evidence-dev7.json)에서는
선택된 Python 테스트 78개와 브라우저 DOM 계약을 통과했다. 별도 wheel을 복사한
DB·자산 workspace와 실제 자식 Worker에서 실행해 저장 화질, 중심/국소 시야
Lens×Sensor RAW, 두 HDR FPN 모드, PWM/LFM, 기본/보정 ISP, 제공된 예측 평가,
화질·렌즈 RAW 데이터셋을 포함한 11개 Job을 완료했다. 주요 소스·정적 화면 파일
20개가 설치본과 바이트 단위로 일치한다. 설치본의 정적 HTTP 응답은 확인했으나
화면의 육안 조작, 물리 카메라 정확도, 대표 해상도 성능은 확인하지 않았다.

인지 경로는 **제공된 예측의 단일 프레임 박스 비교**까지 연결됐다. 실제
detector/segmenter 성능을 얻으려면 가중치의 바이트와 실행 backend, 해당 장면의
독립 정답 box/mask, 좌표 변환 계약과 평가 데이터 분할이 더 필요하다. 빈 라벨을
정답으로 간주하거나 임의 점수를 만들지 않는다.

다음 제품 수용 관문은 센서 입사각 분포와 렌즈 광량/회절/샘플링을 검증한
Lens×Sensor 모델, **그 결합 모델의 동일 RAW·timing을 사용하는 HDR/LFM**,
독립적으로 얻은 FPN·ISP 보정의 적용성 확인, 실제 가중치를 실행하는 인지 평가,
라벨과 분할 중복을 관리하는 데이터셋, 대표 해상도 성능 시험이다. 각 관문은
계산 완료와 실측 정확도를 별도로 보고해야 한다.
