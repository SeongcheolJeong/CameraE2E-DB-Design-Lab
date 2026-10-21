# Camera Virtual Lab

렌즈·센서·ISP를 같은 카메라 구성으로 묶고, 시험 차트의 영상과 Lab 지표를 A/B 비교하는 로컬 작업실이다. 기존 CameraE2E의 저장 광학·센서 응답과 정수 RAW 계산을 사용한다. 외부 데모용 기본 흐름은 **렌즈 교체 → 센서 교체 → 같은 RAW에서 ISP 변경 → 결과 공유**다.

**120° 광각·2MP 확장:** `Wide parametric`은 직선/등거리 투영과 실제 1920×1080 RAW·ISP,
9개 위치의 방향별 SFR을 지원한다. 기존 L1·L2의 시야를 확장한 모델은 아니다.
[광각 설정·SDK·샘플링·적용 범위](camera-platform-virtual-lab-wide-angle.md)를 참고한다.

## 실행

```sh
PYTHONPATH=src .venv-platform/bin/python -m camera_platform serve \
  --workspace "$PWD" --state-dir outputs/camera-platform-virtual-lab-cra-20261011 \
  --port 8780 --empty
```

`http://127.0.0.1:8780/virtual-lab.html`을 연다. 이미 계산한 기준 결과는 저장 이력에서 즉시 열며, **Lab 실행**은 새 취득, **같은 RAW 재처리**는 ISP만 변경한다. 실행 여부·진행 상태·실패 사유를 화면에서 확인하고 취소할 수 있다.

실제 네 카메라 조합과 ISP 두 변형을 미리 준비하려면 다음을 실행한다.

```sh
PYTHONPATH=src .venv-platform/bin/python tools/run_virtual_lab_demo.py --matrix
```

`--showcase`는 네 조합에 일반 광학의 조리개·초점 두 설정, A 광원, 저광량 장면,
같은 RAW의 두 ISP 변형을 더해 10개 데모 결과를 준비한다.

이 명령은 실제 계산을 수행한다. 저장 결과는 API를 통해 다시 열 수 있으며, 결과를 삭제하지 않는 한 발표 때 반복 취득할 필요가 없다. 기본 서버와 HDR 서버의 상태 디렉터리는 변경하지 않는다.

## 5분 데모

1. L1×S1 결과를 기준안 A에 고정한다. 광학→RAW→선형 RGB→ISP를 전환하고 실제 native 크기·공통 관측 화각을 보여준다.
2. 전체 시야 모드에서 L2×S1을 B로 열고 위치별 PSF와 차트 SFR을 비교한다. 저장 기하 PSF의 초점·조리개를 임의로 바꾸지는 않는다.
3. L1×S2를 열어 같은 광량·노출에서 전하·시간 잡음·포화의 차이를 보여준다. DB readout 가정은 상세 정보에 남는다.
4. L1×S1 원본에서 저잡음·고선예도 ISP를 비교한다. RAW가 같은 사실, 잡음/세부 손실과 sharpening overshoot를 함께 확인한다.
5. 보고서를 열고 원본 RAW·배열·설정·해시가 포함된 ZIP을 저장한다.

일반 광학 모델에서는 지원되는 조리개·defocus를 직접 조작한다. DB 렌즈와 같은 제품 모델로 표시하지 않는다.

## 전체 시야 CRA와 픽셀 각도 응답

새 실행의 기본값은 `full_field`와 `physical_relative`다. 저장 렌즈의 시야·파장별
chief-ray 방향을 영상면 좌표로 보간하고, 위치별 PSF와 기하 왜곡을 적용한 광입력에
R/Gr/Gb/B의 상대 각도 응답을 곱한다. 이 계산은 native 픽셀 수광 면적 적분과 RAW
취득 전에 수행된다. 지도를 화면에 얹는 효과가 아니다.

- `field_model.mode`: `full_field` 또는 이전 `center_local`.
- `field_model.horizontal_fov_deg`: `null`이면 공통 Lab 기본 화각 약 5.11°,
  명시하면 2–40° 범위. 렌즈의 저장 시야 지지와 native 계산 예산을 추가로 검사한다.
- `angular_response.mode`: `off`, `physical_relative`, `physical_pupil`, `cosine_power`.
- `angular_response.exponents_r_gr_gb_b`: 명시적 cosⁿ 근사의 네 채널 지수, 각각 0–16.
  물리 상대 응답 모드에서는 이 지수를 사용하지 않는다.

S1/S2에는 저장된 각도별 QE가 없다. `physical_relative`는 기존
`physical_optical_response`의 Fresnel·층 흡수 계산으로 얻은
`EQE(θ,φ)/EQE(0)`를 저장된 정면 QE에 한 번 곱하는 유도 모델이다.
새 FDTD 결과나 해당 제품의 실측 각도 응답으로 표시하지 않는다. 실제 광선 방향은
DB 렌즈에서 오지만 `physical_relative`는 chief ray만 사용한다.
`physical_pupil`을 명시적으로 선택하면 저장 동공 방향의 가중평균을 사용한다.
DB 광학의 투과율·상대 조도 자료가 unity 가정인 점도 유지한다.

### 저장 동공 방향 평균

`physical_pupil`은 저장 DB 렌즈의 각 시야·파장별 동공 방향 묶음에서 기존 층 모델의
상대 각도 QE를 구하고, 유효 광선의 정규화된 가중치로 평균한다. 이 평균은
**수신측 위치별로 광학 응답과 분리해 적용하는 동공 방향 평균**이다. 기존 PSF와 왜곡은
유지하며, 광선의 도달 위치와 입사 방향의 상관까지 함께 반영해 PSF를 다시 만드는
공동 광선·각도 계산은 아니다.

저장된 정면 QE는 한 번만 적용한다. 추가 항은 동공 방향별 `EQE(θ,φ)/EQE(0)`의
조건부 평균이다. 가중치의 합은 1이며, 이를 렌즈 투과율이나 추가 광량으로 다시 곱하지
않는다. 제품 실측 각도 응답, 신규 FDTD, 전하 수집까지 포함한 실측 센서 모델을 의미하지 않는다.

화면에서 **픽셀 입사각 응답 → 층 모델 상대 응답 · 동공 방향 평균**을 선택하고 새로
촬영한다. 동일한 카메라·노출·seed·ISP에서 Chief-ray 결과와 비교하면 각도 분포를
사용한 차이를 확인할 수 있다. 이 모드 변경은 취득 모델을 바꾸므로 같은 RAW의 ISP
재처리로 대체하지 않는다. CRA 지도는 계속 chief-ray 진단이며, 상대 응답 지도와
채널별 곡선이 동공 방향 평균 결과다. 표본 수·유효 광선 수·방향 범위·모델 출처와
동공 응답표의 해시를 결과·오프라인 보고서에 보존한다.

이 선택은 저장 동공 방향 묶음이 있는 DB 렌즈에서 제공한다. 일반 회절 광학의
`analytic` 모델에는 해당 묶음이 없다. 동공 방향 평균을 선택한 뒤 화면에서 일반 광학으로
바꾸면 Chief-ray 모드로 변경하고 그 이유를 표시한다. API 명세에서는 이 조합을 사용하지 않는다.
기본 모드는 계속 `physical_relative`이며 동공 평균으로 자동 승격하지 않는다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780") as client:
    spec = client.virtual_lab_template()["specification"]  # 기본 L1-S1 DB 카메라
    spec["name"] = "Stored-pupil angular response"
    spec["field_model"]["mode"] = "full_field"
    spec["angular_response"]["mode"] = "physical_pupil"
    job = client.submit_virtual_lab(spec)
    result = client.wait_virtual_lab(job["id"])
```

S1의 기존 CFA proxy 두께 3.6 µm는 전체 적층 두께와 혼동된 자료다. 이 연결에서는
원본 DB를 수정하지 않고, 같은 출처의 CFA 범위 0.68–0.87 µm의 중간값 0.775 µm와
잔여 passivation 두께 1.625 µm를 별도의 가정으로 사용한다. S2는 CFA 이름 alias를
정규화한다. 유도 응답표·변경 근거·원본/계산 코드 해시는 결과에 보존한다.

640×360은 공통 표시 격자다. 화각을 늘려도 센서 pitch를 바꾸지 않으므로 실제 native
배열과 계산량이 커진다. 이 Lab adapter의 400만 native 픽셀 상한은 **실행 정책이며
하드웨어 한계가 아니다**. 상한이나 저장 렌즈의 지원 범위를 넘으면 명시적으로 실패한다.
해상도·pitch·화각을 자동으로 낮추지 않는다. 전체 시야는 설정한 관측창 전체를 뜻하며,
저장 렌즈의 최대 image circle 전체를 뜻하지 않는다.

CRA 지도는 550 nm에서 고정 0–60°, 상대 응답 지도는 네 사이트 평균을 고정 0–1.5×로
표시한다. native 수치 배열과 채널별 프로필도 저장한다. 파장별 실제 전하 계산은
450–650 nm 전체를 사용한다. 같은 RAW 재처리는 이 지도와 원본 전하를 유지하고 ISP만 바꾼다.
이전 명세에 새 필드가 없으면 `center_local`/`off`로 해석한다.

광학 입력은 모든 파장의 역왜곡으로 필요한 범위와 전체 PSF 지지 영역을 포함한다.
Color·tone·edge 평가 위치도 원래 차트 위치에서 왜곡된 영상 위치로 옮긴다.
SFR의 cycles/degree 표시는 `FOV/640`의 근축 환산이다. 위치별 왜곡 Jacobian까지
보정한 물체각 MTF나 제품 순위로 해석하지 않는다. 광학 MTF 진단은 550 nm 중심 PSF이며,
주변부 변화는 실제 전체 영상의 다섯 위치 SFR에서 확인한다.

같은 전체 시야 PSF에서 각도 응답만 OFF/ON 비교하려면:

```sh
PYTHONPATH=src .venv-platform/bin/python tools/run_virtual_lab_cra_demo.py --fov 12
```

이 명령은 두 번의 실제 취득과 한 번의 동일 RAW ISP 재처리를 저장한다. 동일 seed·FPN·
노출·ISP를 사용하며, 결과에는 같은 광학 입력에서 각도 응답 적용 전후의 평균 광전자도 남는다.

## 공통 API와 SDK

`GET /api/v1/virtual-lab/template`은 실행 가능한 카탈로그와 기본 명세를 반환한다. `POST /api/v1/virtual-lab/runs`로 시험을 제출한다. `GET .../runs/{id}` 및 공통 `/api/v1/jobs/{id}`로 조회하고 `POST .../runs/{id}/cancel`로 취소한다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780") as client:
    spec = client.virtual_lab_template()["specification"]
    spec.update(name="My camera Lab", test_suite="all")
    acquired = client.submit_virtual_lab(spec)
    result = client.wait_virtual_lab(acquired["id"])
    isp = {**spec["isp"], "denoise": 0.85, "sharpen": 0, "profile": "low_noise"}
    replay = client.replay_virtual_lab(acquired["id"], isp)
    client.wait_virtual_lab(replay["id"])
```

재처리는 `source_run_id`, `name`, `isp`만 받는다. 원본 파일은 전후 해시를 검사하며 광학·취득을 다시 실행하지 않는다. 결과 파일 경로는 해당 작업의 해시 manifest에 있는 파일만 제공한다. `GET .../runs/{id}/bundle`은 오프라인 보고서와 재실행 설정·원본을 묶는다.

## 기본 데모 조건

기준 광량은 1000 cd/m², 노출 4 ms, gain 1이다. 표시 기준과 센서별 D65 분광 보정도 1000 cd/m² 조건에서 고정한다. 광량을 바꾸어도 프레임 최대값에 맞춰 밝기를 다시 정규화하지 않는다. 잡음 시험은 평균 광응답에 0.0625·0.25·1·4·16배를 적용한 뒤 각 단계에서 독립 RAW 8장을 새로 취득한다.

수동 LSC는 native Bayer에서 `1 + strength × normalized_radius²`를 적용하는 명시적 gain 지도이며 자동 보정이나 렌즈 상대 조도 자료가 아니다. Black offset은 저장 black level에 더하는 DN 단위 보정이다. Demosaic은 bilinear로 고정한다.

## 지표의 의미

- 광학 MTF, 선형 카메라 SFR, 최종 ISP SFR을 구분한다. ISP sharpen 이후의 SFR을 렌즈 단독 MTF로 표시하지 않는다.
- 색은 선언한 광원·반사율 기준의 패치 결과와 ΔE00을 제공한다. WB/CCM은 고정하며 평가 차트에 매번 재피팅하지 않는다.
- 450–650 nm로 제한된 D65 기준 백색을 표준 D65 표시 백색으로 고정 변환하고,
  센서별 분광 보정 행렬도 그 백색을 보존한다. 같은 변환을 기준 패치 XYZ에 적용한다.
  A 광원이나 저광량 프레임에 맞춰 보정을 다시 계산하지 않는다.
- 잡음은 독립 반복 취득의 픽셀별 시간 분산으로 계산한다. 공간적 변화·FPN은 별도 항목이다. 비선형 ISP 출력의 잡음은 처리된 반복 프레임에서 계산한다.
- 공통 Lab 관측 화각에서 후보의 native 크기를 조정하고 공통 640×360 화면으로 변환한다. 이는 이전 도로 실험의 30° 전체 관측창과 다르다.
- 전체 시야 모드는 위치별 PSF·왜곡·CRA 진단과 선택한 상대 센서 응답을 결합한다.
  `physical_relative`는 chief ray, `physical_pupil`은 저장 동공 방향의 수신측 가중평균이다.
  중심 국소 모드의 주변부 창은 독립된 국소 시험이다. 두 결과의 관측 위치를 혼동하지 않는다.
- 모델 기반 Lab 기능이다. 제품 실측 일치·표준 인증·독립 물리 검증은 이 데모의 완료 조건이 아니다.

## 빠른 확인

```sh
PYTHONPATH=src .venv-platform/bin/python -m pytest -q tests/platform/test_virtual_lab*.py
```

수치 단위·기본 동작·RAW 재사용·API 작업 lifecycle을 확인한다. 상세 수렴 연구나 전수 DB 검증으로 기능 출시를 지연하지 않는다.
