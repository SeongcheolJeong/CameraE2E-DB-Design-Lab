# Reference ISP parameter tuning

기존 Reference Bench의 보존 RAW에서 **주변부 밝기·시간 잡음·질감·halo의 상충관계**를 탐색한다. 새 취득이나 광학 계산 없이 같은 RAW의 실제 ISP 출력을 바꾸며, 한 장 미리보기와 전체 반복 취득 평가를 구분한다. 기존 6후보 `noise_detail`, 세 demosaic, 색 재보정 recipe는 유지한다.

## 시연 흐름

1. 완료된 source run과, sweep이면 정확한 source candidate를 선택한다.
2. `center` 또는 `outer-ne`의 차트 한 개에서 보존된 첫 RAW를 재처리한다. RAW·LSC·RAW NR·demosaic·detail·ISP 단계를 살펴본다.
3. 후보 축을 고정한 뒤 9개 시야·각 차트의 보존 반복 RAW 전체를 평가한다. 기본 source는 native 256×256 probe, 독립 취득 8회다.
4. 잡음–질감 산점도, 시간 NPS, 밝기·균일도, halo·색차와 탈락 이유를 함께 확인한다.
5. 평가한 후보의 profile을 revision으로 저장하고 동일 source RAW를 재처리한다.

슬라이더 선택만으로 계산 결과를 바꾸지 않는다. 새 preview 또는 study 작업이 완료되면 해당 실행의 영상과 지표를 표시한다. Preview 작업의 결과는 전체 평가용 취득 source로 사용할 수 없다.

## 처리 단위와 조정 범위

`camera_reference_isp_v2`의 기존 처리 순서와 선형 RGGB readout 제한을 유지한다. 잡음 모델은 저장 readout에서 유도한 shot/read/ADC 근사이며 실측 보정값이 아니다. 자세한 기존 계약은 [Reference ISP Lab](camera-platform-reference-isp-lab.md)을 따른다.

| 경로 | 범위 | 의미 |
| --- | --- | --- |
| `raw_denoise.strength` | 0–1 | 같은 CFA 사이트의 기존 신호·잡음 적응 필터 혼합 강도 |
| `raw_denoise.range_floor_dn2` | 1e−12–2⁴⁸ | 필터의 range 가중치 계산에 쓰는 분산 하한, DN² 단위 |
| `linear_sharpen.amount` | 0–1 | 단일 scale detail 증폭 강도 |
| `linear_sharpen.threshold` | 0–1 | 표시 white로 정규화한 선형 RGB의 detail 문턱 |
| `linear_sharpen.max_delta` | 0–1 | 같은 단위의 최대 부호 있는 detail 보정량 |
| `lsc_tuning.strength` | 0–1 | 고정 native LSC map의 보정 강도 |
| `lsc_tuning.gain_cap` | 1–4 | 실제 적용하는 native LSC gain의 상한 |

RAW NR는 각 R/Gr/Gb/B sublattice의 고정 3×3 이웃을 사용한다. 이 조정은 필터 반경 변경이나 별도 luma/chroma NR가 아니다. Bounded sharpen은 하나의 detail scale을 사용하며 다중 scale 복원 또는 deconvolution으로 표시하지 않는다.

새 `lsc_tuning`은 선택 필드다. 생략하면 종전 native map을 수치 그대로 적용한다. 명시적인 `{ "strength": 1.0, "gain_cap": 4.0 }`도 종전 동작과 같다. 다른 값은 다음과 같이 파생한 map을 사용한다.

```text
effective_gain = min(1 + strength × (frozen_gain − 1), gain_cap)
```

원본 calibration은 수정하지 않는다. LSC는 signed black subtraction 이후 한 번만 적용하고, NR의 분산 hint는 실제 적용 gain²을 반영한다. `lsc_enabled=False`이면 보정 map을 적용하지 않는다. 필터/색/표시 단위와 원본 RAW는 그대로 보존한다.

## 한 프레임 미리보기

`POST /api/v1/reference-workshop/isp-previews`는 완료된 취득의 `field_id`·`chart`를 받아 **frame 0 한 장만** 처리한다. 과거 취득에 없는 chirp는 거부한다. 광학·픽셀 재계산, 잡음 재생성 또는 질감용 결정론적 재취득을 호출하지 않는다.

보존 자료는 다음과 같다.

- 정확한 원본 RAW 사본과 SHA-256, 평균 전자율, source reference 및 존재하는 co-located plane.
- source capture·baseline calibration의 hash와 profile, 첫 프레임의 단계별 수치 배열.
- 실제 native RAW/linear/processed/display 영상, 선택한 한 프레임의 지지 영역.
- `preview_scope`, `processed_frame_count=1`, `raw_preserved=true`, `acquisition_reused=true`.

이 결과에는 시간 잡음·NPS·후보 선택·전체 시야 제약 판정이 없다. `metrics`의 해당 값은 `null`이며, 선택 frame의 유효 마스크를 8회 반복의 공통 마스크로 표시하지 않는다. 형식은 기존 Reference result의 `fields` 한 개와 `images[chart]`를 유지한다.

```python
from camera_platform import reference_isp
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=180) as client:
    source_id = "run-34aadb199e4d416bb0ba390ccf79636c"
    source = client.reference_workshop_run(source_id)["result"]["summary"]
    isp = reference_isp.default_profile(source["configuration"]["isp"])
    isp["lsc_enabled"] = True
    isp["lsc_tuning"] = {"strength": 0.95, "gain_cap": 4.0}
    isp["raw_denoise"]["strength"] = 0.2
    isp["linear_sharpen"].update(amount=0.3, threshold=0.01, max_delta=0.05)
    preview = client.preview_reference_isp(
        source_id, isp, field_id="outer-ne", chart="texture"
    )
    finished = client.wait_reference_workshop(preview["id"], timeout=7260)
    if finished["status"] != "succeeded":
        raise RuntimeError(finished.get("error", finished["status"]))
    print(finished["result"]["summary"]["preview_scope"])
```

후보 source인 경우 위 조회에서 `summary["candidates"]`의 정확한 항목을 사용하고 SDK에 `source_candidate_id="candidate-00"`와 같이 전달한다. Preview도 기존 Registry·Worker의 조회·공통 wait·취소를 사용한다.

## 전체 후보 평가

`POST /api/v1/reference-workshop/isp-studies`의 `recipe="parameter_tuning"`은 `parameter_axes` 사전으로 조정 경로와 명시적 후보 값을 받는다. 서로 다른 유한 숫자 배열만 허용하며, 한 축은 1–12개 값을 갖는다. 전체 조합과 기준안을 합쳐 최대 12개 profile을 실행한다. 기준과 완전히 같은 profile은 중복 실행하지 않는다. 새 실행마다 실제 독립 후보 결과가 만들어지며 source를 재취득하지 않는다.

기본 축은 RAW NR `0/0.2/0.4` × LSC strength `0.85/1.0` × sharpen amount `0/0.3`이다. 기본 설정에서 기준안을 포함해 12개 후보다. 기준안은 **full frozen LSC, NR OFF, sharpen OFF**이며 `candidate-00`으로 고정한다.

모든 후보는 Adaptive Laplacian을 사용한다. Source의 WB·CCM·색 calibration·tone·표시 white를 고정하며 이 study에서 색 fitting을 다시 하지 않는다. 명시적 축 외의 NR floor·detail threshold/max-delta·LSC cap은 제출한 profile을 사용한다.

```python
job = client.submit_reference_isp_study(
    source_id,
    "parameter_tuning",
    isp=isp,
    parameter_axes={
        "raw_denoise.strength": [0.0, 0.2, 0.4],
        "lsc_tuning.strength": [0.95, 1.0],
        "linear_sharpen.amount": [0.0, 0.3],
    },
)
finished = client.wait_reference_workshop(job["id"], timeout=7260)
if finished["status"] != "succeeded":
    raise RuntimeError(finished.get("error", finished["status"]))
summary = finished["result"]["summary"]
print(summary["study"]["selection"])
```

위 예제는 같은 열린 `PlatformClient` context에서 실행한다. 조합 12개에 기준 profile이 포함되지 않으면 총 13개가 되므로 요청을 거부한다. 예를 들어 제출 profile의 LSC cap을 3으로 고정할 경우 기준 cap 4와 다른 후보이므로 축 크기를 줄여야 한다.

### 판정 조건

질감의 분모는 **full frozen LSC, Adaptive Laplacian, source color calibration, NR/sharpen OFF**의 같은 잡음 없는 기준이다. 후보마다 LSC를 약하게 만들고 그 어두운 결과를 자신의 기준으로 사용하지 않는다. Dead-leaves highpass 대비와 실제 반복 취득의 시간 잡음을 분리한다.

| 기본 조건 | 의미 |
| --- | --- |
| `texture_retention_min=0.95` | 모든 시야의 고정 기준 대비 질감 보존율 ≥95% |
| `halo_increase_max=0.05` | 각 edge의 overshoot/undershoot가 기준보다 최대 5%p 증가 |
| `delta_e_increase_max=1.0` | 각 시야의 평균 Macbeth ΔE00 악화 ≤1 |
| `brightness_relative_min=0.95`, `brightness_relative_max=1.05` | 각 시야의 uniform 차트 평균 휘도가 기준의 95–105% |
| `uniformity_increase_max=0.05` | 시야 평균 휘도의 min/max 비율이 기준보다 최대 0.05 저하 |
| `noise_improvement_min=0.01` | 변경 후보의 밝기 정규화 잡음 개선 ≥1%를 요구하고, 통과 후보의 절대 시간 잡음 개선도 1% 미만이면 기준 유지 |

밝기를 2% 낮춰 신호와 잡음을 함께 줄인 후보가 단순히 더 낮은 절대 잡음으로 선택되는 것을 막는다. 변경 후보마다 `brightness_normalized_noise = max(시야별 시간 잡음 / 같은 시야 평균 휘도)`를 계산하고, 기준의 같은 값보다 최소 1% 개선되었는지 확인한다. 이 조건을 통과하지 못하면 `brightness_normalized_noise_gain_below_limit`로 탈락한다. 표시 영상 자체를 정규화하는 연산은 아니며 원래의 고정 표시 범위를 유지한다.

이 조건은 수정 가능한 데모 조건이다. 제품 합격 규격이 아니다. 필수 시야·색/edge/질감 지지 영역·밝기 지표가 누락되거나 무효이면 통과시키지 않는다. 밝기·균일도·밝기 정규화 잡음 제약까지 통과한 후보 사이에서는 **가장 불리한 시야의 절대 시간 잡음**이 가장 작은 후보를 선택한다. 기준 결과 자체가 불완전하면 `inconclusive`로 남긴다. NPS는 잡음의 주파수별 성격을 설명하는 진단이며 별도의 자동 선택 점수로 섞지 않는다.

## 시간 NPS 해석

`fields[].tests.isp_lab.noise_power_spectrum` 및 각 `uniform/noise-power-spectrum.json`에 저장한다. 반복 처리 영상에서 **각 pixel의 시간 평균**을 빼 고정 장면을 제거한 뒤, Rec.709 luma와 Hann window로 periodogram을 계산한다. 분모는 `(N−1) × sum(window²)`이며 per-frame 공간 평균을 추가로 빼지 않으므로 프레임 전체가 흔들리는 시간 잡음도 남는다.

계산 ROI는 source 공통 유효 마스크 안의 가장 큰 완전한 사각형이다. 구멍을 0으로 채워 FFT하지 않는다. 최소 8×8 지지를 확보하지 못하면 unavailable이다. 주파수는 cycles/native pixel이며 원형 평균을 0–0.5까지 기록한다. 이 원형 평균은 주파수 대역의 총 power가 아니다. 전체 2D 스펙트럼 적분은 대각선 반경 0.5 초과까지 포함하며, 저장된 window 가중 시간 분산과 비교할 수 있다. 고정 패턴 잡음과 실측 센서 NPS를 대표하지 않는다.

## 실행·저장과 재현

```bash
PYTHONPATH=src .venv-platform/bin/python tools/run_reference_isp_tuning_demo.py
```

실행기는 기준/조정 preview, 기본 전체 study, 선택 profile 저장과 동일 RAW 재처리를 준비한다. 기본 영수증·설정·전달 경로는 `outputs/reference-isp-tuning-20261012`이며 `--source`, `--output`, `--base-url`로 바꾼다. 기존 외부 SSD의 500 GB 프로젝트 예산·잔여 용량 검사를 유지한다. 용량이 부족하면 정확도나 해상도를 자동으로 낮추지 않는다.

용량 집계는 `Path.rglob`의 반복 stat을 `os.scandir`의 DirEntry 기반 순회로 바꾸었다. 검사 시점·500 GB 상한·잔여 공간 조건·파일별 `max(logical bytes, allocated bytes)`는 유지하며 매번 다시 순회한다. Symlink는 거부하고 읽기 오류는 명시적으로 실패한다. 읽기 전용 비교에서 140,920개 파일·5,901개 디렉터리의 49,284,513,792 bytes를 두 구현이 정확히 동일하게 집계했으며, 기존 9.396/10.111초 대비 새 순회는 3.324초였다. 약 2.9배의 **용량 순회** 속도 개선이며 전체 작업이나 preview가 2.9배 빨라졌다는 의미는 아니다.

조회는 `client.reference_workshop_run(id)`, 공통 대기는 `client.wait_reference_workshop(id)`, 취소는 `client.cancel_reference_workshop(id)`다. 저장 profile은 `client.create_reference_revision(selected["configuration"], ...)`로 만들고, `client.replay_reference_workshop(source_id, revision["specification"]["isp"])`로 재실행한다.

Preview의 engine 경로 시간에도 진행 콜백의 용량 검사와 파일 저장이 포함된다. 이 시간과 요청·source hash 검증·대기까지 포함한 관측 시간은 다르다. 준비된 결과를 클릭하는 속도를 새 처리 속도로 표시하지 않는다. `runs.json`의 `submission_seconds`, `observed_wait_seconds`, 결과의 `performance.elapsed_seconds`를 구분하며 이 벽시계 시간을 ISP 하드웨어 지연으로 사용하지 않는다.

기하 PSF·센서 구조·재료·수집·readout의 기존 모델 가정은 유지한다. 새로운 국소 tone mapping, 별도 luma/chroma NR, 잡음 제약 CCM fitting, 실측 제품 calibration은 이 기능의 범위가 아니다.

## 2026-10-12 실제 인도 결과

[준비된 ISP 튜닝 데모](http://127.0.0.1:8780/reference-workshop.html?run=run-1d0f23b7936247368b6d4d060d8a11e3&candidate=candidate-04&baseline=run-1d0f23b7936247368b6d4d060d8a11e3&baseline_candidate=candidate-00&field=outer-ne&chart=texture&stage=isp&editor=isp)는 **12개 후보 × 9개 시야 × 4개 차트 × 보존 RAW 8회**의 실제 재처리 결과다. 별도 한 장 preview 두 개와 선택 프로필의 전체 재실행도 완료했다. Source는 `run-34aadb199e4d416bb0ba390ccf79636c`, native 256×256, D65·1000 cd/m²·4 ms·gain 1이다.

| 결과 | 실제 수치와 판정 |
| --- | --- |
| 선택 | `candidate-04`: Adaptive Laplacian, RAW NR 0.2, floor 1 DN², full LSC·cap 4, sharpen OFF |
| 최악 시야 시간 잡음 | 0.007858 → 0.007240, **7.87% 감소**; 선형 sRGB luma 표준편차 |
| 최소 질감 보존율 | **95.73%**, 공통 잡음 없는 bypass 기준 |
| 시야별 밝기 비율 | 기준의 0.999986–1.000017; 표시 범위 변경 없음 |
| 최대 색차 악화 | 평균 Macbeth ΔE00의 시야별 증가 최대 0.00527 |
| 최대 추가 halo | overshoot/undershoot 증가 최대 0.0235%p |
| 강한 NR 반례 | `candidate-05` NR 0.4는 잡음이 약 15.55% 감소하지만 최소 질감 91.86%로 탈락 |
| 약한 LSC 반례 | `candidate-01`은 절대 잡음이 줄어도 밝기 대비 잡음 개선이 약 0.0066%에 그쳐 탈락 |

이는 고정된 12개 후보와 데모 조건 안의 선택이다. 독립 장면 일반화 또는 모든 파라미터의 최적점을 입증한 결과가 아니다.

선택 프로필은 `reference_design_revision-593be8e99fb04d7da47664a54e3e707d`에 저장했다. 같은 source에서 실행한 `run-3609f19bcd7b47e095623c8caa47dba8`의 RAW 288개와 처리 배열 36개가 선택 후보와 정확히 일치했다. 전체 검증에서 NPS 108개, manifest 파일 37,981개, RAW 사본 3,746개의 연결을 확인했으며 상태는 `verified`다. 이 사본 수는 새로운 독립 취득 수가 아니다.

관련 Python 검사 **102개**, 기존 ISP Lab 및 새 튜닝 UI의 Node 검사, JavaScript 문법 검사가 통과했다. 실제 브라우저에서도 후보 변경에 따른 NR 값·영상·지표 연결, 기준안 유지, ISP 직접 링크, 편집값과 보존 결과의 구분을 확인했다. 선정 후보의 HTTP native RAW 다운로드도 SSD 파일의 hash와 같았다.

| 실제 관측 비용 | 제출부터 완료 확인까지 |
| --- | ---: |
| 기준 한 장 preview | 46.70초 |
| NR 0.2 + sharpen 0.3 한 장 preview | 32.03초 |
| 전체 12개 후보 평가 | 1,708.73초, 약 28.5분 |
| 저장한 선택 프로필의 전체 재실행 | 224.17초 |

이 시간은 해당 로컬 실행의 원본 검증·SSD 검사·처리·저장·polling을 포함한다. 실시간 ISP 지연이 아니다. 최종 네 작업의 manifest 범위는 논리 **7,519,358,140 bytes (7.519 GB)**, SSD 할당 **12,113,805,312 bytes (12.114 GB)**다. 원본 전용 자산과 취소한 이전 시도는 이 합계에서 제외하며, 프로젝트 용량 검사는 이들도 포함한다.

- [전달 명세](../outputs/reference-isp-tuning-20261012/delivery.json)
- [전체 파일·NPS·재실행 검증](../outputs/reference-isp-tuning-20261012/verification.json)
- [저장한 프로필](../outputs/reference-isp-tuning-20261012/saved-profile.json)
- [실제 ISP A/B 화면](../outputs/reference-isp-tuning-20261012/isp-demo.png)
- [후보 선택과 탈락 이유 화면](../outputs/reference-isp-tuning-20261012/candidate-selection.png)

재검사는 `PYTHONPATH=src .venv-platform/bin/python tools/verify_reference_isp_tuning_demo.py`로 실행한다. `runs.json`은 이전 시도의 이력을 남기며 `delivery.json`은 최종 네 작업만 연결한다. 8780 서버는 `.venv-platform`과 현재 editable source를 사용한다.
