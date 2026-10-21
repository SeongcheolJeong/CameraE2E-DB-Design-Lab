# Virtual Lab · 120° 광각 카메라

`wide_parametric`은 **1920×1080 native 가상 센서 영역**에서 장면 → 광학 → 픽셀 → RAW → ISP → Lab을 실행하는 광각 설계 모델이다. 화면의 640×360 영상은 미리보기다. Full native ISP PNG와 RAW·평균 응답·측정 배열은 결과에 별도로 보존한다.

저장 DB 렌즈를 120°까지 외삽하지 않는다. 선택 센서의 pixel pitch·저장 QE·readout은 재사용하고, 렌즈는 선언한 투영·F-number·회절·방사/접선 blur·투과율·상대 조도·출사동공으로 구성한다. 제품 렌즈 처방이나 특정 센서의 제품 고유 mode를 나타내지 않는다.

## 화면에서 실행

1. `/virtual-lab.html`의 Lens에서 **wide_parametric 광각 모델**을 선택한다. 수평 FOV 120°, F/2.8, 전체 시야, 동공 방향 평균이 초기값으로 들어간다. 기존 DB 렌즈와 저장 결과는 바뀌지 않는다.
2. Rectilinear 또는 Equidistant를 선택한다. **각도 격자**는 같은 전방 평면의 직선이 각 투영에서 어떻게 보이는지 확인하는 입력이다. 해상력·24색·균일장·명암 계단·합성 도로 입력도 사용할 수 있다.
3. 가상 촬영을 실행한다. 결과 상단에 실제 H/V/D FOV·초점거리·pitch·native 크기가 표시된다. 해상력은 9개 위치와 방사/접선 방향을 선택한다. CRA와 Angular gain 단계에서 공간 응답을 확인한다.
4. 현재 결과를 기준안 A로 고정하고 다른 구성을 촬영한다. 조작한 설정이 저장 결과와 다르면 이전 결과라는 안내가 표시된다.
5. ISP만 바꾸면 **같은 RAW로 ISP 재처리**를 사용할 수 있다. 투영·FOV·동공·blur·광량·샘플링 설정이 달라지면 새 취득이 필요하다.
6. **Native ISP**로 1920×1080 결과를 열고, **원본 ZIP**으로 RAW·수치·오프라인 보고서를 가져온다. 실패/취소된 광각 취득은 실행 기록에서 연 뒤 **체크포인트에서 재개**할 수 있다. 명세·코드·모델·타일 해시가 달라졌으면 재사용을 거부하며 새 촬영이 필요하다.

완료된 결과를 다른 브라우저에서도 정확히 비교하려면 `/virtual-lab.html?run=<B 실행 ID>&baseline=<A 실행 ID>`를 사용한다. `run`은 현재 설계안 B, 선택적인 `baseline`은 기준안 A다. 지정한 실행이 없거나 완료되지 않았으면 오류를 표시하고 다른 결과로 대체하지 않는다. 이 링크에서는 다른 진행 작업의 완료가 A/B를 자동 교체하지 않는다. 파라미터가 없으면 기존 최신 결과 열기 동작을 유지한다.

이번 120°·S1 각도 격자 비교의 저장 링크는 [Rectilinear A / Equidistant B](http://127.0.0.1:8780/virtual-lab.html?run=run-7e4fba1ac086498884a07e31e260b945&baseline=run-68b9dccf2fde45edb108348d58d647c7)다. 같은 서버의 실행 저장소가 있어야 열 수 있다.

## 명시적인 구성

| 항목 | 설정과 의미 |
|---|---|
| `field_model.horizontal_fov_deg` | 2–120°. 지정 FOV와 native 폭·pitch로 초점거리를 계산 |
| `projection` | `rectilinear`: r=f·tanθ, `equidistant`: r=f·θ |
| `native_shape_row_col` | `[1080,1920]` 고정. 자동으로 작은 RAW를 확대하지 않음 |
| `exit_pupil_distance_mm` | 기본 6 mm. 센서면 CRA와 선언한 출사동공 방향을 결정 |
| `radial_blur_um`, `tangential_blur_um` | 기본 0 µm. 회절에 추가하는 명시적 blur 근사 |
| `transmission` | 0.001–1. 기본 1. 절대 광량의 선언한 투과 조건 |
| `relative_illumination_power` | 0–8. 기본 0. 선언한 cosⁿ 상대 조도 모델 |
| `sampling` | `auto`, `2`, `4`, `8`. 광학·픽셀 입력 표본화; 실제 사용값과 refinement 진단은 결과에 보존 |
| `tile_size` | 기본 256 native pixels. 짝수 타일로 RGGB 좌표 유지 |
| `max_working_bytes` | 기본 4 GiB 작업 메모리 정책 |
| `min_free_disk_bytes` | 기본 10 GiB 여유 디스크 정책 |

자원 예산은 **소프트웨어 실행 정책**이다. 하드웨어의 절대 한계나 완료 시간 보증이 아니다. 부족하면 명시적으로 실패하며 화각·pitch·native 해상도·요청 표본화를 몰래 낮추지 않는다. 실행 시간과 실제 저장량은 완료된 결과에서 확인한다.

## SDK 예제

저장소 환경에서 서버가 실행 중일 때 다음을 실행한다. `test_suite="all"`은 전체 Lab 시험을 요청한다. 첫 연결만 확인하려면 `"resolution"`으로 명시적으로 범위를 줄일 수 있다.

```python
from copy import deepcopy
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780") as client:
    spec = deepcopy(client.virtual_lab_template()["specification"])
    spec.update(
        name="120 deg rectilinear · 2MP Lab",
        lens_id="wide_parametric",
        sensor_id="S1",
        scene="grid",
        test_suite="all",
        illuminant="D65",
        luminance_cd_m2=1000,
        exposure_ms=4,
        gain=1,
        field_model={"mode": "full_field", "horizontal_fov_deg": 120},
        angular_response={
            "mode": "physical_pupil",
            "exponents_r_gr_gb_b": [2, 2, 2, 2],
        },
        optics={"f_number": 2.8, "defocus_um": 0},
        wide_angle={
            "projection": "rectilinear",
            "native_shape_row_col": [1080, 1920],
            "exit_pupil_distance_mm": 6,
            "radial_blur_um": 0,
            "tangential_blur_um": 0,
            "transmission": 1,
            "relative_illumination_power": 0,
            "sampling": "auto",
            "tile_size": 256,
            "max_working_bytes": 4 * 1024**3,
            "min_free_disk_bytes": 10 * 1024**3,
        },
    )
    job = client.submit_virtual_lab(spec)
    finished = client.wait_virtual_lab(job["id"], timeout=14400)
    if finished["status"] != "succeeded":
        raise RuntimeError(finished)
    print(client.virtual_lab_run(job["id"])["result"]["summary"]["report_url"])

    isp = {**spec["isp"], "profile": "detail", "denoise": 0, "sharpen": 1.2}
    replay = client.replay_virtual_lab(job["id"], isp, name="Same native RAW · detail ISP")
    client.wait_virtual_lab(replay["id"], timeout=14400)
```

작업 조회·대기·취소는 기존 공통 lifecycle을 따른다. 실패 또는 취소된 **취득 작업**을 재개할 때는 `client.resume_virtual_lab(stopped_run_id)`로 새 작업을 만든 뒤 `client.wait_virtual_lab(resumed["id"])`로 기다린다. 보존한 RAW의 ISP 재처리와 미완료 취득의 체크포인트 재개는 별개다.

## 측정과 모델 범위

- **광학 → 픽셀 → ISP:** 광학 PSF, 수광 면적 적분, 전하·잡음·ADC, bilinear demosaic·WB·CCM·수동 LSC·denoise·sharpen·tone을 구분한다. ISP 변화는 같은 RAW에서 비교할 수 있다.
- **9개 native 해상력 위치:** 방사·접선 edge를 각각 측정한다. Native SFR은 `cycles/native pixel`로 보존한다. ROI 내 각도 척도 변화가 5% 이내이면 해당 위치의 구면 투영 Jacobian으로 `cycles/degree`도 제공한다. 단순 `FOV/640` 환산이 아니다. 5%를 넘으면 native 곡선의 국소 각도 환산값은 제공하지 않으며, 현재 화면과 요약표는 native 측정과 각도축 제한을 표시한다.
- **별도 각도 재표본화 진단:** 위 제한에 해당하는 ROI에는 균일한 접평면 각도 격자로 다시 표본화한 `angular_resampled` 결과를 별도로 저장한다. 원래 ROI 안의 표본만 bilinear 보간하며 `linear_system`·`processed_system`의 `cycles/degree` 곡선과 MTF50, 각도 격자 간격, 원본 ROI 내 지지 여부를 기록한다. 지원 격자를 만들지 못하면 `unavailable`과 사유를 남긴다. 이 결과는 보간의 영향을 포함하는 **angular-resampled (bilinear) 진단**이며 native SFR을 대체하거나 native 세부 정보를 복원한 값이 아니다. 전체 수치 JSON의 `tests.resolution.sites.<site>.radial|tangential.angular_resampled`에서 확인한다.
- **색·잡음·명암:** 선언한 장면과 광원 아래에서 고정 보정으로 평가한다. 반복 취득의 시간 분산을 사용하고 장면 질감을 잡음으로 대체하지 않는다. CNR·인지·AEB 성능을 자동으로 추정하지 않는다.
- **CRA와 픽셀 입사각 응답:** `physical_relative`는 chief ray의 층 모델 상대 응답이다. `physical_pupil`은 **선언한 합성 출사동공 원판**의 방향별 상대 응답을 수신측 위치마다 가중평균한다. DB 렌즈의 저장 광선 묶음과 다른 출처임을 기록한다. 저장 정면 QE는 한 번만 적용하고 조건부 동공 가중치는 합을 1로 정규화한다. 별도 투과율을 다시 곱하는 동공 처리로 해석하지 않는다.
- **픽셀 결합의 한계:** 동공 평균은 수신측 위치별 분리 근사다. PSF의 광선 도달 위치와 입사 방향을 함께 다시 푸는 joint 모델이나 실측 QE 정합 모델은 아니다. CRA 지도는 동공 평균 모드에서도 chief-ray 진단이다.
- **표시 범위:** CRA는 0–60°, 상대 각도 응답은 0–1.5× 고정 색맵이다. 수치 배열은 색맵 범위로 잘라내지 않는다. Native ISP 원본과 640×360 미리보기를 함께 제공한다.

이번 기능의 성과는 넓은 시야·센서·ISP의 상호작용을 같은 작업실에서 실제 계산하고 비교하는 것이다. 파라미터 광학 모델의 결과를 특정 상용 렌즈의 성능, 실측 일치 또는 인증 결과로 표시하지 않는다.

## 2026-10-11 실행 근거와 재실행

실제 1920×1080 취득으로 S1/S2 × rectilinear/equidistant의 전체 Lab 4개, S1의 투영 비교용 격자+해상력 2개를 완료했다. S1 rectilinear 원본의 ISP 재처리 1개까지 총 7개 저장 결과의 manifest·파일 해시, RAW/평균 응답/native ISP 크기, 9개 위치×2방향 SFR을 확인했다. 재처리는 원본 RAW·평균 응답 해시를 유지했고 native 처리 영상이 변경됐다.

전체 Lab에는 24색 패치와 5개 광량×8회 반복 취득의 시간 잡음 측정이 포함된다. 4개 조합 모두 대표 위치의 4×/8× 비교를 통과해 4×를 선택했다. 이는 저장된 대표 입력에 대한 refinement 결과이며 모든 입력의 정확도 보증은 아니다.

| 실제 실행 범위 | 소요 시간 | 저장 자료 |
|---|---:|---|
| 격자+해상력 1개 조합 | 약 7.2분 | 약 606 MB |
| 전체 Lab 1개 조합 | 약 17.1–18.3분 | 약 3.3 GB |
| 같은 RAW의 ISP 재처리 | 약 6.3초 | 취득 자료를 재사용하고 파생 ISP를 별도 저장 |

시간은 32 GiB/10 CPU 장비에서 여러 작업을 병렬 실행한 이번 관측값이다. 전체 Lab의 프로세스 최대 RSS는 약 1.71–1.80 GB였다. 저장량은 보존 파일의 논리적 합으로, 재처리 hardlink를 포함한 실제 디스크 점유와 다르다. 이 값을 다른 설정의 실행 시간이나 메모리 상한으로 보장하지 않는다.

- 파일 검증: `outputs/virtual-lab-wide-delivery-20261011.json`
- SDK 실행 기록: `outputs/virtual-lab-wide-demo-20261011/runs.json`, `grid-runs.json`
- 브라우저 확인: `outputs/virtual-lab-wide-demo-20261011/ui-wide-browser-qa.json`
- 관련 회귀: `tests/platform/test_virtual_lab*.py` 109개 통과. Ruff와 JavaScript 문법 검사 통과.

```sh
PYTHONPATH=src .venv-platform/bin/python tools/run_virtual_lab_wide_demo.py
PYTHONPATH=src .venv-platform/bin/python tools/verify_virtual_lab_wide_delivery.py \
  --runs outputs/virtual-lab-wide-demo-20261011/runs.json \
         outputs/virtual-lab-wide-demo-20261011/grid-runs.json \
  --state-dir outputs/camera-platform-virtual-lab-cra-20261011 \
  --output outputs/virtual-lab-wide-delivery-20261011.json
```

첫 명령은 기본적으로 전체 Lab 4개와 ISP 재처리를 제출하며 동일 명세·코드·자산의 완료 작업은 재사용한다. 격자 결과를 새로 만들 때는 `--sensors S1 --scene grid --suite resolution --no-replay --output outputs/virtual-lab-wide-demo-20261011/grid-runs.json`을 지정한다. API 서버와 해당 state directory가 먼저 실행돼 있어야 한다.
