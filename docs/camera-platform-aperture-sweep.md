# 조리개 단계별 RAW·ISP 설계 비교

휘도·노출을 바꾸는 후속 기능은 [조건별 조리개 선택](camera-platform-aperture-conditions.md)을 따른다.

같은 Canon p0248 특허 처방과 IMX462 구조 응답에서 조리개만 바꾸고,
중심·outer-ne의 선예도, 광전자, 시간 잡음 및 SNR을 함께 비교한다.
기본 단계는 F/2.75, 3.2, 4.12, 5.6, 8, 11이다. F/2.75 표시는 기존 source stop을
보존하며 실제 모델 F-number를 별도로 표시한다. F/4.12는 특허의 기준 파장
587.6 nm 조건이고, 그 밖의 단계는 사용자가 지정한 가상 설계 조건이다.
특허가 이 모든 조리개 모드나 실물 성능을 제공한다고 주장하지 않는다.

각 단계는 같은 물리 stop을 PSF와 광량 계산에 적용한다. 출사동공 OPD를 사용한
스칼라 회절, 동공 평균 픽셀 각도 응답, 독립 취득 및 고정 ISP를 실행한다.
기존 두 조리개의 광학 캐시는 동일한 해시 계약 아래 재사용할 수 있지만,
새 sweep의 RAW는 명시한 노출·seed로 생성하고 이전 실행은 보존한다.

## 고정 조건과 지표

- D65, 1000 cd/m², 4 ms, gain 1, 독립 취득 8회.
- 2.9 µm native pitch, 중심·outer-ne 각각 256×256 탐침.
- 해상력·색·균일장·질감의 네 차트, 같은 CCM·demosaic·표시 범위.
- LSC, NR, sharpen, FPN은 모든 후보에서 OFF.
- 선예도: 처리 후 차트 SFR50, cycles/native pixel. 광학 단독 MTF와 구분한다.
- 광량: 균일장 Green 사이트의 잡음 전 평균 광전자. 상대량은 같은 시야의
  가장 낮은 요청 F-number를 기준으로 한다.
- 시간 잡음: 동일 Green 사이트에서 반복 취득의 픽셀별 시간 분산을 구한 뒤
  평균하여 RMS로 환산한다. 사진의 공간 변화를 잡음으로 계산하지 않는다.
- SNR: 같은 ROI의 광전자 / 입력 환산 시간 잡음, dB는 20 log10(SNR).
- 포화: 모든 차트와 반복 취득에서 well/ADC/출력 high 포화의 보수적 상한.
  중복 mask의 합은 실제 union으로 표시하지 않는다.

## 선택 규칙

기본 데모 제약은 두 시야 모두 SNR 25 dB 이상, 상대 광전자 10% 이상,
포화 상한 1% 이하다. 필수 지표가 누락되거나 유효하지 않으면 탈락한다.
통과 후보 중 더 불리한 시야의 SFR50가 가장 높은 후보를 선택하며,
동률이면 더 높은 최소 SNR, 그다음 더 작은 F-number를 우선한다.

이 조건은 제품 합격 기준이 아니다. 사용자는 화면에서 제약을 바꿔 **이미 계산된
결과**를 다시 선택하고 실행 ID·조건·선택 후보가 포함된 JSON을 저장할 수 있다.
조건 변경은 새로운 광학·RAW 계산을 의미하지 않는다. 새 sweep을 제출하면
요청한 단계와 제약이 실행 명세에 고정된다.

## 실행

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=120) as client:
    recipe = client.db_design_options()["recipes"]["aperture_sweep"]
    job = client.submit_db_design_study(recipe)
    result = client.wait(job["id"], timeout=14400, poll_interval=2)
    assert result["status"] == "succeeded", result.get("error")
    print(result["result"]["summary"]["result_url"])
```

API는 기존 `/api/v1/db-design/studies`, 조회·취소·파일·bundle은 기존 Reference
Workshop lifecycle을 사용한다. 원본과 `aperture-selection.json`이 같은 파일 명세에
포함된다. SSD의 500 GB 프로젝트 예산과 남은 용량을 확인하며, 부족하거나 광학
수렴에 실패하면 해상도·허용 오차를 몰래 완화하지 않고 실패를 기록한다.

## 적용 범위

좁은 조리개에서는 rear surface 전체를 덮는 낮은 밀도의 역방향 적분 격자가
실제 통과 동공을 충분히 표본화하지 못할 수 있다. F/11 초기 검사에서 이 문제가
나타났다. 처리 자원의 상한과 수치 오차 허용치는 다른 조건이며, 자원을 늘려도
수렴 검사는 그대로 통과해야 한다. 탐색 결과에는 실제 사용 표본 수·광량 적분
변화·방향 분포 수렴 및 별도 계산 시간을 보존한다.

이것은 명시한 차트·광량·노출·센서·ISP와 두 시야에서의 상대적 설계 선택이다.
임의 장면, 다른 광량, 전체 시야의 최적 조리개나 실측 제품 성능을 보장하지 않는다.
native source-cell 중심 입력의 한계와 스칼라·분리된 픽셀 각도 응답 가정은
[회절 모델 설명](camera-platform-aperture-diffraction.md)을 따른다. 연속 렌즈 MTF나
Nyquist까지의 일반 Scene 정확도와 혼동하지 않는다.

## 2026-10-17 실제 데모

실행: `run-9692f54b37ab4c5d80f4e9edbd4e83dd`.

[단계별 비교 화면](http://127.0.0.1:8780/db-design.html?run=run-9692f54b37ab4c5d80f4e9edbd4e83dd&candidate-a=candidate-01&candidate-b=candidate-03&field=outer-ne&chart=resolution&stage=isp)
및 [보존 보고서](http://127.0.0.1:8780/api/v1/reference-workshop/runs/run-9692f54b37ab4c5d80f4e9edbd4e83dd/files/report.html).

| 요청 F-number | 중심 / 주변 SFR50 (cycles/native pixel) | 중심 / 주변 SNR (dB) | 최소 신호 유지 | 기본 선택 |
|---|---|---|---|---|
| 2.75 | 0.119 / 0.048 | 30.948 / 29.999 | 100.00% | 통과 |
| 3.2 | 0.159 / 0.093 | 29.530 / 28.852 | 73.92% | 통과 |
| 4.12 | 0.211 / 0.158 | 27.081 / 26.811 | 44.77% | 선택 |
| 5.6 | 0.198 / 0.190 | 23.925 / 23.519 | 24.30% | SNR 미충족 |
| 8 | 0.193 / 0.171 | 19.872 / 19.422 | 11.93% | SNR 미충족 |
| 11 | 0.172 / 0.143 | 15.830 / 15.379 | 6.31% | SNR·신호 미충족 |

모든 후보의 측정된 high 포화 상한은 0%였다. 최소 신호 유지는 각 시야의
F/2.75 대비 Green 평균 광전자 비율 중 작은 값이다. 이 표의 선예도는
처리 후 SFR50이며 광학 단독 MTF가 아니다.

기본 25 dB 조건에서는 F/4.12를 선택했다. 최소 SNR만 23 dB로 바꾸면
F/5.6이 선택된다. F/5.6은 F/4.12보다 주변부 SFR50이 약 20.2% 높지만
중심 SFR50은 약 6.2% 낮고, 최소 SNR은 3.29 dB 낮다. F/8·11에서는
두 시야의 SFR50과 SNR이 F/5.6보다 모두 낮았다. 이는 위 고정 조건의
모델 결과이며, 다른 조도·노출·센서·ISP의 최적값을 뜻하지 않는다.

시연 순서:

1. 저장된 25 dB 조건의 F/4.12와 세 곡선을 확인한다.
2. 최소 SNR을 23 dB로 바꾸어 F/5.6의 이득과 손실을 확인한다.
3. 표의 `F/5.6 비교`를 눌러 같은 실행의 원본 RAW·ISP를 연다.
4. `선택 명세 저장`으로 실행 ID·manifest 해시·변경한 조건을 저장한다.
5. `저장 조건 복원`으로 기본 선택으로 돌아간다. 조건 변경·저장은 재촬영하지 않는다.

검증 근거는 `outputs/db-design-aperture-sweep-20261017/verification.json`,
`tests-final.log`, `ui-actual-result.log`, `browser-proof.json`에 보존한다.
84개 집중 테스트, 252개 회절 노드, 77개 광량 적분 구간이 통과했다.
F/8의 두 구간은 더 촘촘한 표본으로 재시도했으며 기존 허용오차를 유지했다.
448장의 native 256×256 정수 RAW(보정 기준 64장 포함)와 2,985개 파일을
보존하고 해시를 확인했다. 기존 두 조리개의 각각 64장 RAW는 이전
`run-a3c36ff4c9d24b39966a929836e107ae` 결과와 비트 단위로 일치한다.

결과 패키지는 1,619,178,348 bytes이다. 광학 캐시가 준비된 이번 실행의
엔진 기록은 332.33초, SDK 제출부터 완료까지는 412.36초였다. 광학 준비는
별도로 수행했고 네 병렬 작업의 각 새 조리개 계산은 약 16.3–18.7분이었다.
이 시간은 시뮬레이터 실행 비용이며 카메라의 취득·처리 지연이 아니다.
SSD 전체 파일의 용량 검사도 실행 비용에 포함된다.
