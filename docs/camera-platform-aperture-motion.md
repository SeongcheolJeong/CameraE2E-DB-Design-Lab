# 광량·물체 속도별 조리개와 노출 선택

`aperture_motion`은 보존된 수차·회절·픽셀 각도 응답에 노출 중 수평 이동을 결합한다.
DB Design Lab에서 광량 × 속도 조건을 선택하고 같은 조건의 조리개·노출 후보를
RAW·고정 ISP·처리 후 SFR·시간 SNR·포화로 비교한다. 기존 정적 `aperture_conditions`
실행과 결과는 유지한다.

행별 시간과 기하 변형을 비교하는 새 경로는 [Global / Rolling Shutter 데모](camera-platform-rolling-shutter-demo.md)를 참조한다.

## 고정 모델과 새 입력

- 원본: `run-9692f54b37ab4c5d80f4e9edbd4e83dd`, Canon p0248 처방 × IMX462 구조 모델.
- F-number: 2.75·3.2·4.12·5.6·8·11. 실제 적용 F-number와 특허값은 계속 구분한다.
- 기본 장면 휘도: 10·100·1000·10000 cd/m², D65. 입사 조도 lux가 아니다.
- 기본 대상 속도: 0·1·4 m/s, 거리 20m. 고정 카메라 앞의 수평 등속 이동이다.
- 후보 노출: 1·4·16 ms, global shutter, gain 1, 독립 취득 8회.
- 중심·outer-ne의 native 256×256; 해상력·색·균일장·질감 차트.
- 셀마다 조리개 6개 × 노출 3개 = 18후보. 전체 12셀·216후보·13,824 RAW.
- 기존 CCM·WB·demosaic·tone·표시 white 유지. FPN·LSC·NR·sharpen OFF.

물체 속도의 영상면 환산은 `velocity_px_s = focal_length_m / pixel_pitch_m * speed_m_s / distance_m`다.
모든 조리개에 동일한 원본의 550nm paraxial 초점거리 약 24.73288mm와 pitch 2.9µm를
사용한다. 조리개별 다른 파장 초점거리를 섞어 이동량이 달라지지 않는다.
20m·4m/s에서는 1·4·16ms에 약 1.71·6.82·27.29픽셀을 이동한다.
이 환산은 선언한 pinhole 근사이며 주변부의 실제 왜곡 미분, 자차 운동, 깊이 변화가 아니다.
거리는 속도를 환산하는 입력이며 원본 차트의 크기나 렌즈 초점 조건을 다시 계산하지 않는다.

## 노출 중 움직임과 취득

원본의 잡음 없는 전체 R/Gr/Gb/B 평균 전자율 plane 각각에 시간 적분을 적용한 뒤,
고정 RGGB 사이트를 선택하고 광전하·암전류·shot/read noise·포화·ADC를 다시 계산한다.
이미 취득한 RAW나 Bayer mosaic에 공간 blur를 적용하지 않는다. 평균 전자율에는 광량,
광전하 계산에는 노출이 각각 한 번 적용된다. 픽셀 개구는 원본 광학 응답에 포함되어 있어
추가 적용하지 않는다.

이동 적분은 노출 중앙을 기준 위치로 둔 대칭 등속 경로다. native plane을 구간 선형으로
재구성하고 그 삼각 basis를 노출 구간에서 해석적으로 적분한다. 표본 수를 임의로 줄여
속도를 높이는 경로가 없고, 0속도는 원본 평균 응답을 그대로 유지한다.
이는 선언한 native 보간 모델 안의 정확한 시간 적분이다. 연속 장면의 subpixel PSF나
Nyquist까지의 물리적 정확도를 새로 확보했다는 의미는 아니다.

저장된 plane은 256×256이고 바깥 입력은 보존되어 있지 않다. 전체 연구의 가장 빠른 속도와
가장 긴 노출의 이동 지지 영역·ISP 경계를 제외한 **동일한 유효 사각형**을 모든 조건·후보에
적용한다. 미지원 가장자리는 영상에 표시하고 정량 지표에서는 제외한다. 중앙 edge ROI를
지원하지 못하는 설정은 계산 범위를 임의 축소하지 않고 거부한다.

## 공동 선택 규칙

두 시야 모두 시간 SNR ≥25dB, high 포화 상한 ≤1%를 만족한 후보 중 더 불리한 시야의
처리 후 SFR50이 가장 높은 조리개·노출을 고른다. 동일하면 최소 SNR, 짧은 노출,
낮은 F-number 순으로 선택한다. 유효하지 않은 SFR·포화에 의해 억제된 분산·누락 지표는
통과로 처리하지 않는다. 통과 후보가 없으면 미결정으로 남긴다.

상대 광전자율은 같은 시야·같은 노출의 최대 개방을 기준으로 삼는다. 조리개 투과 손실과
노출시간에 따른 신호 증가를 혼동하지 않는다. A/B 기본 기준은 F/2.75·4ms이며,
사용자가 해당 노출을 제출하지 않으면 첫 노출을 사용한다.
균일장 SNR은 질감을 잡음으로 해석하지 않는 반복 취득 시간 분산이다. 고포화율은
동일 유효 영역에서 well/ADC/output high 비율을 합산해 1로 제한한 보수적 상한이다.
고정 ISP 표시 white의 clipping은 별도 지표다.

## 실행과 보존

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=180) as client:
    job = client.submit_aperture_motion(
        "run-9692f54b37ab4c5d80f4e9edbd4e83dd",
        luminance_cd_m2=[10, 100, 1000, 10000],
        object_speed_m_s=[0, 1, 4], object_distance_m=20,
        exposure_ms=[1, 4, 16],
    )
    result = client.wait(job["id"], timeout=14400, poll_interval=10)
    assert result["status"] == "succeeded", result.get("error")
    print(result["result"]["summary"]["result_url"])
```

```sh
PYTHONPATH=src .venv-platform/bin/python tools/run_db_design_demo.py \
  --comparison aperture_motion \
  --source-run run-9692f54b37ab4c5d80f4e9edbd4e83dd \
  --luminance-cd-m2 10 100 1000 10000 \
  --object-speed-m-s 0 1 4 --object-distance-m 20 --exposure-ms 1 4 16 \
  --output outputs/db-design-motion-20261017
```

POST `/api/v1/db-design/studies`와 공통 조회·대기·취소·파일·bundle을 사용한다.
축은 중복 없는 오름차순 1–8개 값이다. 휘도 0.1–100000 cd/m², 노출 0.1–100ms,
속도 0–50m/s, 거리 1–1000m를 받으며, 추가로 실제 이동 지지 영역을 검사한다.
최대 24지도 셀, 36셀/노출 조합, 216카메라 평가의 명시적 자원 상한이다.
이 상한은 하드웨어의 일반적 한계가 아니다. 외장 SSD 500GB 프로젝트 예산을 유지한다.

원본 응답/manifest 해시, 전기 모델, calibration, 이동 커널·속도·거리·노출,
유효 영역, 평균 전자율, 8회 정수 RAW, ISP와 후보별 전체 결과를 보존한다.
화면용 요약은 큰 중복 배열을 제외한 index이며 원본 계산 자료는 별도 artifact로 남긴다.
화면에서 한계값을 바꾸는 것은 저장 지표의 재선택이다. 새 속도·거리·노출은 명시적 제출과
완료된 새 실행을 거쳐야 실제 결과로 표시한다.

본 결과는 두 시야의 local shift-invariant 모델과 고정 차트에 한정된다. 물체/배경의
독립 움직임, 가림, rolling shutter, 3D 장면, 실제 렌즈 왜곡에 따른 이동 궤적,
실물 카메라 검증이나 제품 최적 설정으로 확대하지 않는다.

## 준비된 실제 데모 · 2026-10-17

완료 실행은 `run-b314ad8b029f4af585247f615c7f21c5`이며, 원본 조리개 탐색 및 이전 정적
광량·노출 실행은 덮어쓰지 않았다. [움직임 A/B 데모](http://127.0.0.1:8780/db-design.html?run=run-b314ad8b029f4af585247f615c7f21c5&condition=condition-007&candidate-a=candidate-04-e02&candidate-b=candidate-03-e01&field=outer-ne&chart=resolution&stage=isp)는
1000 cd/m²·1m/s에서 정지 조건의 선택안 F/5.6·16ms와 이동 조건의 선택안 F/4.12·4ms를 비교한다.
모든 후보는 이 링크에서 같은 **이동 조건**으로 계산된 영상이다.

기본 선택 조건(SNR 25dB 이상, 포화 상한 1% 이하)에서 얻은 결과:

| 장면 휘도 | 정지 | 1m/s | 4m/s |
| --- | --- | --- | --- |
| 10 cd/m² | 통과 후보 없음 | 통과 후보 없음 | 통과 후보 없음 |
| 100 cd/m² | F/2.75 · 16ms | F/2.75 · 16ms | F/2.75 · 16ms |
| 1000 cd/m² | F/5.6 · 16ms | F/4.12 · 4ms | F/4.12 · 4ms |
| 10000 cd/m² | F/5.6 · 1ms | F/5.6 · 1ms | F/5.6 · 1ms |

F/2.75는 원본 개방 조건의 이름이며 실제 적용값은 약 F/2.751이다. 거리 20m는 속도의
영상면 환산 조건이다. 1000 cd/m²·1m/s에서 F/5.6·16ms → F/4.12·4ms로 바꾸면
이동 길이는 6.823 → 1.706 native px, 더 불리한 시야의 처리 후 SFR50은
0.07993 → 0.13016 cycles/native pixel, 시간 SNR은 30.44 → 26.82dB로 변한다.
두 설정 모두 high 포화 상한은 0%다. 짧은 노출이 번짐을 줄이는 대신 신호·SNR을 잃는 사례다.

100 cd/m²에서는 정지에서 1m/s로 바뀔 때 worst SFR50이 0.04817 → 0.04936으로
소폭 증가한다. 보존 RAW로 재현되는 낮은 SNR의 sampled-chart/ISP SFR 추정 결과이며,
움직임이 광학 MTF를 개선했다는 근거로 쓰지 않는다. 4m/s에서는 0.03610으로 감소한다.
선택은 이 고정 차트와 모델의 보존 지표에 한정되며 작은 차이에 통계적 유의성을 주장하지 않는다.

### 계산·보존·검증 기록

- 독립 native RAW 13,824장, 216후보·432시야 결과. SSD 묶음 29,815,880,055bytes(29.82GB).
- SDK 제출부터 완료까지 967.2초; 계산 엔진 보고 시간 747.3초. 차량 처리 지연과 무관한 로컬 실행 시간이다.
- SSD: `/Volumes/T7/CameraE2E/reference-workshop/run-b314ad8b029f4af585247f615c7f21c5`.
- 결과 manifest SHA-256: `850bbd942fc645c113662f7826ee2dfed3dbffc3d489a0a061f7ecb41cf4f96c`.
- 신규 68,920파일과 원본 2,985파일의 해시를 검증했다. 정지 RAW 4,608장이 이전 정적
  조건 실행과 bit-identical이며, 균일장 RAW 2,304장의 속도 간 동일성을 확인했다.
- 48개 평균 응답을 별도 시간 적분으로 검증하고 RAW·ISP를 재생했다. 추가 192개 RAW에서
  24개 SFR/ESF/LSF 곡선을 다시 계산해 저장 결과와 일치를 확인했다.
- 관련 회귀 145개 통과. 실제 12조건·216후보의 화면 바인딩, 20/30dB 정책 재선택 24건을
  Python과 대조했다. 브라우저에서 RAW/ISP 전환, 미결정/밝은 조건 선택, RAW 및 선택 명세
  다운로드를 확인했다.

증거와 SDK 재실행 진입점은 `outputs/db-design-motion-20261017/`에 있다:
`run_motion.py`, `delivery.json`, `capture-timing.json`, `verification.json`,
`sfr-verification.json`, `ui-actual-result.log`, `browser-proof.json`,
`browser-map.png`, `browser-ab.png`, `tests-final.log`.
