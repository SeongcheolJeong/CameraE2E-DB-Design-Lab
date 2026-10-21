# 장면 휘도·노출별 조리개 선택

움직임 번짐과 조리개·노출 공동 선택은 별도
[광량·속도 조건 지도](camera-platform-aperture-motion.md)를 사용한다.
이 문서의 정적 실행과 이전 결과는 유지된다.

완료된 회절 조리개 탐색의 보존 평균 광전자율을 이용해 저광량부터 밝은 입력까지
새 RAW·ISP를 계산한다. 광학 처방·stop·회절·픽셀 각도 응답·센서·CCM은 원본 실행에
고정하고, 휘도와 노출만 바꾼다. 기존 정수 RAW를 밝기 배율로 확대하지 않는다.

## 기본 비교

- 원본: `run-9692f54b37ab4c5d80f4e9edbd4e83dd`의 F/2.75·3.2·4.12·5.6·8·11.
- 장면 휘도: 10·100·1000·10000 **cd/m²**. 물체면 입사 조도(lux)가 아니다.
- 노출: 1·4·16 ms; gain 1, D65, global shutter, 독립 취득 8회.
- 중심과 outer-ne의 실제 native 256×256; 해상력·색·균일장·질감 차트.
- 총 12조건 × 6조리개 × 2시야 × 4차트 × 8회 = **4608개 정수 RAW**.
- 같은 전자→RGB 행렬, WB·demosaic·tone·표시 white. 자동 밝기 보정 없음.
- FPN·LSC·NR·sharpen은 원본과 동일하게 OFF. 센서 구조·readout 근사의 범위 유지.

장면은 정적이다. 노출 증가에 따른 평균 광전자·암전류·잡음·포화는 반영하지만,
움직임 번짐이나 AE를 최적화하지 않는다. 같은 휘도×노출 곱이라도 암전류와
취득 잡음은 각 노출의 전기 계산을 거치며, 조건별 결과를 재사용한 RAW로 대체하지 않는다.

## 재사용과 보존

원본 manifest, result, 센서 전기 모델, 공통 calibration, 각 시야의 평균/4채널 전자율,
차트 기준과 광학 진단 이미지를 해시로 확인한다. 계산에 사용하는 byte가 바뀌면 거부한다.
평균 전자율에는 휘도 비율만 곱하고, 노출시간은 전하 취득에 한 번 적용한다.
원본 실행은 수정하지 않으며 새 실행에 source run·manifest·사용 자산 해시를 남긴다.

후보 간에는 같은 seed 열을 사용한 쌍대 비교를 유지한다. 각 차트의 8회 반복은 서로
다른 seed의 독립 reset 취득이다. 조건마다 별도 RAW·취득 metadata·ISP·지표를 보존한다.
광학 캐시 전체를 조건마다 복제하지 않는다. 최대 네 후보를 병렬 처리하고 조건별로
500GB 외장 SSD 프로젝트 예산을 검사한다. 공간 부족·취소·실패를 성공으로 처리하지 않는다.

## 지표와 선택

각 조건에서 두 시야 모두를 평가한다. 기본 제한은 시간 SNR ≥25dB, high 포화 상한 ≤1%다.
최대 개방 대비 상대 광전자 하한은 기본 0%다. 기존 고정노출 탐색의 10%를 그대로 쓰면
밝은 장면에서도 F/11을 항상 제외하므로 조건 맵에서는 해제한다. 사용자는 다시 지정할 수 있다.

조건을 통과한 후보 중 더 불리한 시야의 **처리 후 SFR50**이 가장 높은 조리개를 고른다.
광학 단독 MTF와 구분하며, 입력·ISP의 공통 유효 영역 밖인 edge ROI는 선택에 사용하지 않는다.
동률이면 최소 SNR이 높은 후보, 그다음 실제 F-number가 낮은 후보를 고른다.
어떤 후보도 통과하지 못하면 미결정으로 남긴다. 저조도 실패를 임의의 추천으로 바꾸지 않는다.

신호와 시간 분산은 같은 균일장 Green 사이트에서 구한다. well·ADC·출력 포화로 분산이
눌린 SNR은 censored/inconclusive로 표시하고 선택에서 제외한다. 포화율은 네 차트·8반복의
well/ADC/output high fraction 합을 1로 제한한 보수적 상한이며 정확한 mask union이 아니다.
고정 표시 white에 의한 영상 clipping은 센서 RAW 포화와 별도 지표로 보존한다.

## 화면

DB Design Lab의 **광량 × 노출 · 조건별 선택 지도**에서 셀을 누르면 해당 조건의
후보·RAW·ISP·선예도·SNR·포화·탈락 이유가 함께 전환된다. 비교 A/B는 같은 실행과
같은 조건에서만 선택한다. URL의 `condition=condition-NNN`으로 정확한 셀을 다시 연다.

선택 한계값을 바꾸면 보존 지표로 전체 맵을 임시 재선택한다. 이는 재촬영이 아니다.
선택 명세 JSON은 실행·manifest·조건·변경 정책과 모든 셀의 결정을 포함한다.
새 휘도·노출 축은 명시적 계산 버튼을 눌러야 실행된다.

## SDK / CLI

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=120) as client:
    job = client.submit_aperture_conditions(
        "run-9692f54b37ab4c5d80f4e9edbd4e83dd",
        luminance_cd_m2=[10, 100, 1000, 10000],
        exposure_ms=[1, 4, 16],
        selection_policy={"min_snr_db": 25, "min_relative_signal": 0,
                          "max_saturation_fraction": 0.01},
    )
    result = client.wait(job["id"], timeout=14400, poll_interval=2)
    assert result["status"] == "succeeded", result.get("error")
    print(result["result"]["summary"]["result_url"])
```

```sh
PYTHONPATH=src .venv-platform/bin/python tools/run_db_design_demo.py \
  --comparison aperture_conditions \
  --source-run run-9692f54b37ab4c5d80f4e9edbd4e83dd \
  --luminance-cd-m2 10 100 1000 10000 --exposure-ms 1 4 16 \
  --output outputs/db-design-conditions-20261017
```

API는 `POST /api/v1/db-design/studies`이며 `comparison_mode=aperture_conditions`,
`source_run_id`, `luminance_cd_m2`, `exposure_ms`, `selection_policy`를 지정한다.
각 축은 오름차순의 중복 없는 1–8개 값이다. 범위는 0.1–100000 cd/m²,
0.1–100 ms이며 최대 24개 조합을 지원한다. 이는 명시한 계산 자원 상한이며,
하드웨어나 물리 모델의 일반적인 한계는 아니다.
기존 작업 조회·wait·cancel·Reference Workshop files/bundle을 공용으로 사용한다.

선택 결과는 두 시뮬레이션 시야와 고정 차트에 한정한다.
실물의 조도 사양, 전체 시야의 최적값, 동체 촬영, 제품 합격이나 안전성 보증으로 확대하지 않는다.

## 2026-10-17 실제 준비 결과

완료 실행은 `run-08c20c6eb15d486f889382f516ad4966`이다.
[조건별 조리개 데모](http://127.0.0.1:8780/db-design.html?run=run-08c20c6eb15d486f889382f516ad4966&condition=condition-007&candidate-a=candidate-01&candidate-b=candidate-03&field=outer-ne&chart=resolution&stage=isp)에서
12조건의 실제 RAW·ISP와 선택 근거를 확인한다. 기본 정책 결과는 다음과 같다.

| 장면 휘도 (cd/m²) | 1 ms | 4 ms | 16 ms |
|---|---|---|---|
| 10 | 통과 후보 없음 | 통과 후보 없음 | 통과 후보 없음 |
| 100 | 통과 후보 없음 | 통과 후보 없음 | F/2.75 |
| 1000 | 통과 후보 없음 | F/4.12 | F/5.6 |
| 10000 | F/5.6 | F/8 | 통과 후보 없음 |

이는 SNR 25dB·high 포화 상한 1%를 모두 만족한 후보 중 두 시야의 최소 SFR50을
최대화한 결과다. 어두운 조건은 SNR 또는 지표 유효성 때문에, 가장 밝은 16 ms는
포화와 edge 유효 영역 때문에 통과하지 못했다. 한계값을 바꾸면 선택은 달라질 수 있다.

4608개 정수 RAW를 외장 SSD에 보존했다. 패키지 내 manifest 대상 파일은
10,130,287,063 bytes(약 10.13 GB)이며, SDK 제출부터 완료까지 659.0초가 걸렸다.
시간에는 SSD 용량 검사·해시 검사·저장이 포함된다. 광학 PSF는 원본 실행에서 재사용했다.

- 원본 패키지: `/Volumes/T7/CameraE2E/reference-workshop/run-08c20c6eb15d486f889382f516ad4966`
- 재실행 SDK: `outputs/db-design-conditions-20261017/run_conditions.py`
- 완료 기록: `outputs/db-design-conditions-20261017/delivery.json`
- 실제 비용: `outputs/db-design-conditions-20261017/capture-timing.json`
- 회귀 결과: `outputs/db-design-conditions-20261017/tests-final.log` — 119 passed
- 실제 결과의 화면 선택 동등성: `outputs/db-design-conditions-20261017/ui-actual-result.log`
- 독립 결과 검증: `outputs/db-design-conditions-20261017/verification.json` — 신규 22,696개와
  원본 2,985개 파일의 해시, 144개 시야별 지표 재계산, 144개 RAW 재취득 및 ISP 재처리 일치.
  기준 1000 cd/m²·4 ms의 384장 RAW는 기존 조리개 탐색과 비트 단위로 동일하다.

저광량 edge의 SFR 추정기가 수치적으로 실패할 수 있어, 해당 경우만 `unavailable`로
기록하고 RAW·ISP는 보존하도록 수정했다. 이 지표를 0이나 통과값으로 대체하지 않는다.
실패했던 첫 실행도 별도 기록으로 유지했다.

대형 결과의 영상 조회 병목도 수정했다. 동일 요청 안의 run·artifact 결합 검증을 한 번으로
합치고, evidence 복원용 64MiB 캐시를 작은 literal과 큰 node의 LRU로 나눴다.
요청 간 검증 캐시는 사용하지 않으며, 실제 파일 해시·semantic SHA·audit 검사를 유지한다.
관련 회귀는 `tests-serving.log`의 301 passed이다. 최종 서버에서 A/B PNG를 동시에 조회해
각각 HTTP 200·manifest 해시 일치와 8.65초/8.34초를 확인했다(`file-api-proof.json`).
파일 한 장을 열 때도 대형 결과의 무결성을 검사하므로 첫 조회에는 수초가 걸린다.
화면은 이 대기 시간을 `보존 영상 확인 중`으로 표시한다. 조건 전환·정책 변경·실제
선택 명세 다운로드·256×256 A/B 이미지 로드 확인은 `browser-proof.json`에 남겼다.
화면 증거는 `demo-aperture-conditions.png`와 `demo-aperture-ab.png`이다.
