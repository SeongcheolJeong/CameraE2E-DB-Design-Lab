# Reference Ghost Lab

기존 특허 렌즈 처방에서 **두 번 반사한 광선의 센서면 도달 위치·광량·입사각**을 계산하고, 같은 Reference 센서와 고정 ISP에 연결한다. 렌즈 단면의 반사 경로, 전체 센서면 광학 지도, 실제 native 탐침의 RAW·ISP를 구분해 보여준다. v2는 검은 배경·암부 저대비 차트와 지도에서 선택한 위치의 native 취득을 추가한다. 기존 v1 명세와 저장 결과는 종전 의미를 유지한다.

## 사용

8780 서버의 `/reference-ghost.html` 또는 Reference Bench 상단 **Ghost Lab** 링크를 연다. 저장된 연구를 선택하고 입사각·광량·가상 반사 조건을 바꾼다.

- 준비 각도에서 광학 응답을 재사용한다. 중간 각도는 선형 보간된 **광학 미리보기**이며 정량 결과가 아니다. 범위 밖은 외삽하지 않는다.
- 광량 입력의 단위는 450–650 nm 대역 전체의 **광선에 수직인 면의 입사 irradiance, W/m²**다. 기존 장면에 추가하는 D65 평행광원이며 원본 영상의 밝은 픽셀에서 역추정한 값이 아니다.
- 준비 데이터와 정확히 같은 각도·광량·반사 조건·시야·차트이면 보존 RAW 비교가 표시된다. 일치하는 RAW가 없으면 이전 영상을 지우고 계산 제출을 요구한다.
- **현재 조건으로 RAW 계산**은 새 광전자 평균에서 독립 반복 취득을 수행한다. **같은 RAW · 표시 재처리**는 이전 취득의 전자·정수 RAW를 보존하고 ISP만 다시 실행한다.
- 전체 지도는 512×512 광학 진단이고, RAW는 저장된 시야 또는 사용자가 선택한 위치의 실제 2.9 µm pitch, 256×256 탐침이다. 한 탐침의 폭은0.7424 mm다. 확대 표시는 추가 공간 정보가 아니다.
- 회색·자홍 체크는 무효·포화로 재구성이 유효하지 않은 영역이다. 저장 PNG 자체를 분석할 때는 동봉된 valid mask를 함께 사용한다.

## v2 고스트 전용 차트와 위치 선택

| 입력 | 배경과 광원 | 주 진단 |
|---|---|---|
| `black` — 검은 배경＋밝은 광원 | 배경 광자는0. 밝은 광원은 렌즈 광선 추적에서 직접광·고스트로 공급 | 광원 OFF 대비 평균·최대 전자 상승, 고스트 위치·형상, 포화 |
| `dark_contrast` — 암부 단계＋저대비 물체 | D65 기준 white의0.2·0.5·1·2% 회색 단계, 각 단계에5·10·20% 더 밝은 물체 | 고정 물체/배경 영역의 signed 대비, 시간 잡음, CNR, 광원 OFF 대비 변화 |

광원은 차트 픽셀에 그려 넣지 않는다. 직접 광원상은 선택한 native 탐침 바깥에 있고 고스트만 안으로 들어올 수도 있다. 차트의 주변 입력도 같은 해석적 좌표식으로 생성하므로, native 경계에서 패턴을 잘라 붙이거나 입력을0으로 채우지 않는다.

v2 취득은 같은 위치·노출·색 변환·표시 범위의 세 비교군을 보존한다.

1. **광원 OFF (`source_off`)**: 차트 배경만 취득한다.
2. **직접광만 (`direct_only`)**: 배경에 추가 광원의 직접 투과를 더한다.
3. **직접광＋고스트 (`with_ghost`)**: 배경·직접 투과·2회 반사를 모두 센서 계산 전에 더한다.

각 군은 평균 전자율에서 noise·well·ADC를 새로 계산한다. `black`의 광원 OFF에도 센서 암전류·잡음은 남는다. 저장된 v1의 두 비교군에 광원 OFF를 사후 생성해 끼워 넣지 않는다.

**선택 위치 촬영 순서:** 직접광 또는 고스트 지도를 클릭하거나 X/Y 좌표를 입력한다. 사각형은 선택한0.7424 mm native 수신 영역이다. 정확히 일치하는 저장 결과가 있으면 표시하고, 없으면 native 영상·지표를 비운다. **선택 위치 RAW 계산**을 눌러 제출한 뒤 완료된 run에서 원본 정수 RAW와 고정 ISP를 확인한다. 위치 선택이나 광학 미리보기 갱신만으로 새 RAW 취득이 끝난 것으로 표시하지 않는다.

화면의 A/B 선택기로 세 군 중 둘을 비교한다. **처리 단계**는 고정 ISP 또는 **정수 RAW·ADC 고정 범위**를 고른다. RAW 미리보기는 black pedestal을 포함한0–ADC white DN, gamma1이며 프레임별 최대값에 맞춰 밝기를 늘리지 않는다. 표시용 PNG와 별개로 원본 integer RAW·signed 전자·선형 RGB·유효 마스크는 배열 다운로드에 보존한다. 구형 v1 결과에 RAW 미리보기가 없으면 이를 명시하고 원본 배열 다운로드 또는 새 취득으로 안내한다.

### 위치 지원 범위와 보간 의미

- 전체 광학 지도는 X/Y 각각±13.66 mm다. 검은 배경은 배경 PSF가 필요 없으므로 지도 안에서 지원하며, **256×256 탐침 전체가 지도 안에 있어야 한다**. 따라서 중심 좌표는 각 축±13.2888 mm 이내다.
- 암부·저대비를 비롯한 비검정 배경은 보존된 중심·중간·주변 시야의 실제 chief-image 반경 안에서만 지원한다. 현재 source의 최대 중심 반경은 **11.3277034185 mm**다. 밖을 선택하면 검은 배경으로 전환하거나 탐침을 안쪽으로 옮기도록 명시적으로 거부한다. 더 바깥의 배경에는 새 optical field solve가 필요하다.
- 임의 위치의 배경 PSF는 기존 반경 노드 사이에서 보간하고 방위각에 맞춰 회전한다. 파장별 PSF를 공통550 nm 기준 원점에서 회전해 색수차 중심 이동을 보존하며, 회전한 receiving-cell 질량을 에너지·centroid 보존 방식으로 분배한다. 유한 픽셀 셀의 정확한 회전은 근사다.
- 배경의 광량과 absolute QE는 해당 반경 노드의 pupil 평균 응답을 보간한다. object angle도 보존된 image-height 대응에서 보간해 실제 prescription의 direct transmission을 계산한다. 가장 가까운 시야로 좌표를 바꾸거나 지원 반경 밖으로 외삽하지 않는다.
- **추가 광원의 직접광·고스트는 선택한 실제 XY 수신 영역에 적분**한다. 위의 PSF 보간은 차트 배경에만 적용한다. 각 ROI 안에서는 기존 local-field 근사를 유지하며, 전체센서 왜곡 warp나 새 위치의 정밀 재광선 추적을 뜻하지 않는다.
- 센서면+Y는 위쪽, native 배열 행 증가 방향은−Y다. CFA 원점은 각 탐침의 독립 가상 RGGB 원점이며, 물리 센서 full-frame의 CFA 절대 위상을 복원했다는 뜻은 아니다.
- v2 준비 데모와 임의 위치의 세 비교군은 모두 **LSC OFF**다. 임의 위치에서 가까운 시야의256×256 LSC를 빌려 사용하지 않는다. custom probe에 LSC ON을 요청하면 거부한다. 기존 v1의 고정 LSC 결과는 바꾸지 않는다.

### 암부 지표의 해석

12개 물체와 각 물체의 배경 마스크는 입력에서 고정한다. 선형 ISP 휘도에서 signed 대비를 `(물체 평균−배경 평균)/abs(배경 평균)`으로, signed CNR을 평균 차이를 물체·배경의 픽셀별 시간 분산 합의 제곱근으로 나누어 계산한다. 잡음은8회 반복의 시간 분산이며 차트 질감의 공간 분산이나 ROI 평균의 표준오차를 대신 쓰지 않는다.

부호를 유지하므로 고스트 때문에 명암 순서가 뒤집히면 음수로 나타날 수 있다. 광원 OFF 대비 대비 보존율과 각 군의 CNR은 수치 진단이며, 사람이 물체를 볼 확률·검출기의 정확도·인지 성공률이 아니다. 고정 object/background ROI 중 일부라도 모든 반복에서 유효하지 않거나 포화되면 영역을 줄여 성공으로 계산하지 않고 미판정으로 남긴다. 검정 차트의 평균·최대 전자 상승도 같은 좌표의 광원 OFF 평균을 빼며 부호를 잘라내지 않는다.

## 물리·수치 범위

기준 처방은 기존 Reference의 `US10310255B2 · p0014 · intermediate`, revision `6c48e967-74b4-5901-8186-ba6fafe4797b`다. 원본 DB를 변경하지 않는다. 기존 DB 변환기의 3-band 제한 안에서 7번 변환해 450–650 nm/10 nm의 실제 재료 굴절률을 얻는다. 3색 굴절률을 임의로 21색으로 보간하지 않는다.

- 구면·평면·기존 even-asphere의 동일 sag·도함수로 교차를 계산한다. 미지원 비축 구조로 자동 근사하지 않는다.
- 직접 투과와 렌즈 면 쌍의 2회 반사 경로를 구분한다. stop은 반사면이 아니다. 역방향에서는 입사/출사 매질을 교환하며 Snell 법칙과 개구를 다시 적용한다.
- 입력 동공의 삼각형 면적을 입사 방향에 투영해 광량을 배분한다. 출력 삼각형의 면적으로 나누어 센서면 광량 밀도를 계산한다. 각 고스트를 합계 1로 정규화하지 않는다.
- 격자는 기본 축별64, 명시적으로32/64/128을 선택한다. 각 축 표본 수가 최종 통과 광선 수는 아니다. 원형 림 표본과 삼각형을 함께 저장한다.
- native 수신 픽셀에서는 2×2 면적 quadrature를 사용한다. subcell 삼각형은 centroid deposit으로 처리한다. 이 근사는 sampling을 늘려도 반드시 수렴한다고 가정하지 않는다.
- 수치 교차 실패, 개구 차단, 전반사 제외를 구분한다. 실패 경로를 고스트 에너지 0이라는 물리 결론으로 해석하지 않는다.
- 고스트 광선 자체의 sensor incidence angle로 기존 planar EQE를 조회한다. 0–60° 지원 영역 밖 기여는 제외 진단을 남긴다. `unsupported_angle_weight_diagnostic`는 ray-weight 진단 합계이며 정확한 누락 에너지 비율이 아니다.
- 대표 각도(15°에 가장 가까운 제출 각도)를 축별2배 격자로 재계산한다. 각 시야·반사 조건의 ghost electron-rate 상대 L1 차이5%를 데모 수렴 조건으로 비교한다. 미달하면 `inconclusive`이며 나머지 각도의 정확도를 인증하지 않는다.

반사 조건은 무코팅 비편광 Fresnel 근사, 면당0.5%, 면당2%의 상수 power reflectance다. 상수 반사율은 실제 코팅 설계가 아니며, 전반사에서는 세 프로필 모두 reflectance1을 적용한다. 광선이 통과할 때 같은 조건의 `1−R`을 적용한다. 편광 상태를 표면 간 전파하는 Jones/Mueller 해석은 아니다.

**배경과 추가 광원을 구분한다.** 추가 광원은 직접광·고스트 모두 joint landing-position/direction transport다. 기존 배경은 같은 시야의 통과 동공 평균 분광 투과율을 원본 차트의 분광 광량에 적용하고 기존 PSF·각도 QE로 다시 계산한다. 따라서 배경의 코팅 유래 pupil apodization과 PSF 형상 변화는 이번 모델에 없다. 새 고스트를 최종 RGB에 합성하거나, 센서면 고스트에 렌즈 PSF를 또 적용하지 않는다.

## 취득·측정

Source는 기존 chirp 포함 Reference `run-d2105e38b3cd4f83b3550ebff21ca7fd` 및 고정 manifest다. 기준 D65·1000 cd/m², 4 ms·gain1·25°C·선형 RGGB·8회 취득을 유지한다. v1 준비 결과는 색과 edge이며 uniform·texture도 선택 가능하다. v2는 위 기준 D65 white를0 또는 선언한 암부 비율로 조절한 `black`·`dark_contrast` 차트를 **신규 취득**한다. 원본 Reference에 없던 차트를 단순 RAW 재처리라고 부르지 않는다.

직접광만/고스트 포함은 같은 전기 모델·FPN realization·seed schedule을 사용한다. 광량마다 photo/dark shot noise, read noise, well clipping과 ADC를 실제로 계산한다. 동일 seed는 비교 재현을 위한 pairing이며 동일 물리 촬영이라는 뜻이 아니다.

같은 연구 안에서는 LSC 정책·전자→색 행렬·Bilinear·표시 white·gamma를 고정한다. v1 기본은 기존 LSC ON, v2 준비 데모는 OFF다. v2의 표시 white0.05는 암부 시연을 위한 사전 고정 표시 범위이며 전자 취득을 증폭하지 않는다. NR·sharpen·프레임별 최대값 정규화는 없다. 원본 RAW·signed 전자·선형 RGB·유효/포화 마스크·배경/직접/고스트 평균 전자율을 별도로 저장한다.

지표는 시간 잡음·평균 광전자·포화, 고정 interior ROI의 10/90-percentile 대비, 처리 후 SFR, 색 패치의 직접광 대비 ΔE00 변화다. 색차는 실측 정답 대비 정확도가 아니다. 무효 패치가 있으면 전체 평균 ΔE00를 반환하지 않는다. 자연/고스트 구조의 공간 분산을 시간 잡음으로 부르지 않는다. `fixed_roi_percentile_contrast` 원시 진단값은 zero-filled 처리 배열의 고정 ROI 통계이므로, 유효 비율이1 미만이면 화면에서 대비 판단을 미판정으로 표시한다. 원본 JSON을 사용할 때도 같은 유효성 조건을 적용해야 한다.

## API·SDK·재실행

- `GET /api/v1/reference-workshop/ghost-studies`: 명세 template와 연구 목록
- `POST /api/v1/reference-workshop/ghost-studies`: 계산 또는 `reprocess_of` RAW 재처리 제출
- `GET /api/v1/reference-workshop/ghost-studies/{id}`: 상태·결과·진행
- `GET /api/v1/reference-workshop/ghost-studies/{id}/files/{path}`: 해시 검증 파일
- `GET /api/v1/reference-workshop/ghost-studies/{id}/bundle.zip`: 보존 결과 묶음

기존 `reference_workshop` job kind를 사용하므로 공통 SDK wait/cancel을 그대로 쓴다. 재처리는 name·ISP 외의 취득 설정을 바꾸면 거부한다.

```python
from camera_platform.sdk import PlatformClient
with PlatformClient('http://127.0.0.1:8780', timeout=120) as client:
    spec = client.reference_ghost_studies()['template']
    spec['angles_deg'] = [15.0]
    run = client.submit_reference_ghost_study(spec)
    done = client.wait_reference_workshop(run['id'], timeout=14500)
    assert done['status'] == 'succeeded', done.get('error')
```

현재 template는 `camera_reference_ghost_study_v2`다. API에서 원래의 `camera_reference_ghost_study_v1` 명세도 계속 받는다. 임의 위치는 `probes`에 명시하며, saved field와 함께 최대4개 custom probe를 제출할 수 있다.

```python
with PlatformClient('http://127.0.0.1:8780', timeout=120) as client:
    spec = client.reference_ghost_studies()['template']
    spec.update(fields=[], charts=['black', 'dark_contrast'],
                angles_deg=[15.0], irradiance_w_m2=[1.0],
                probes=[{'id': 'probe-01', 'xy_mm': [6.75, 6.80]}])
    spec['isp']['lsc_enabled'] = False
    run = client.submit_reference_ghost_study(spec)
```

```bash
PYTHONPATH=src .venv-platform/bin/python tools/run_reference_ghost_lab.py
PYTHONPATH=src .venv-platform/bin/python tools/run_reference_ghost_lab.py --angle 15 --output outputs/ghost-15
PYTHONPATH=src .venv-platform/bin/python tools/run_reference_ghost_lab.py --reprocess run-REPLACE --white 2 --output outputs/ghost-replay
PYTHONPATH=src .venv-platform/bin/python tools/run_reference_ghost_lab.py --probes-demo --output outputs/reference-ghost-probes-20261012
```

`--probes-demo`는15°·면당0.5% 반사, 광량1·10 W/m², 중심과 저장 지도에서 정한 peak/ring 두 좌표, `black`·`dark_contrast`, LSC OFF, 표시 white0.05를 제출한다. 총12개 조건에서 세 군을 신규 취득하는 실행 명세다. `--reprocess`와 동시에 사용할 수 없다. 이미 보존된 취득의 ISP만 바꾸려면 `--reprocess`를 별도로 사용한다.

외장 SSD `/Volumes/T7/CameraE2E/reference-ghost-cache`에 반사 광선 및 맵 캐시를 보존한다. 처방·입사각·파장·동공 sampling·코드 해시가 광선 캐시를, 추가로 방위각·센서 응답·수신 격자가 맵 캐시를 고정한다. 완료 manifest가 있고 모든 해시가 맞을 때만 재사용한다. 각도 캐시 단위로 재사용하며 작업의 부분 RAW 취득을 완료 결과로 게시하지 않는다.

기존500GB 프로젝트 예산·남은 공간 검사를 유지한다. 계산 예산 초과 시 입력 해상도를 자동으로 낮추지 않는다. Cache에 저장한 반사 ray bundle은 별도 대용량 프로젝트 자산이며, 연구 bundle에는 지도·상위 경로·native 취득·명세를 포함한다.

## 후속

이번에는 하우징/차광 CAD와 BSDF, 렌즈 가장자리 산란, 센서 반사 왕복, 픽셀 회절·간섭, 실제 코팅, ghost-aware ISP, 인지 평가를 구현하지 않는다. 현재 데모의 물리 validation은 `not_established`다. 이 기능을 출하 렌즈의 절대 고스트 성능 예측으로 사용하지 않는다.

## 기존 v1 준비 데모 순서

8780 서버의 [준비 연구](http://127.0.0.1:8780/reference-ghost.html?run=run-43828e6e4b0d48ef8e7a82ed550750fe)를 연다.

1. 15°, 면당0.5%, center/color에서 직접광과 고스트 포함 RAW의 고정 ISP 영상을 비교한다.
2. 반사 경로 목록을 선택해 센서면 지도와 실제 대표 반사 광선을 연결한다. 경로 선택은 광학 진단이며 아래 RAW는 전체 고스트를 포함한다.
3. outer-ne로 변경해 같은 입사 광원이 주변부 신호·색에 주는 영향을 본다. resolution은 처리 후 SFR을 표시한다.
4. 입사각을15.25°로 변경하면 광학 지도만 보간되고 RAW 영상·지표는 사라진다. 15°로 되돌리면 저장 결과가 표시된다.
5. 면당2% 또는 무코팅을 비교한다. 포화·무효 영역이 있으면 색·대비 결론을 내리지 않는다.
6. 표시 white를 바꾸고 같은 RAW 재처리를 제출한다. 완료 후 별도 run에서 원본 RAW 해시가 동일한지 확인한다.

광원 각도·세기의 조작만으로 새 잡음 취득이 완료된 것으로 표시하지 않는다. ‘현재 조건으로 RAW 계산’은 명시적으로 작업을 제출하며, 처음 보는 조건은 캐시 생성 시간이 필요하다.

### 2026-10-12 실제 준비 실행

준비 run은7개 각도×3반사 조건×2시야×2차트=84개 A/B이며,1344장의 정수 RAW를 취득했다. 계산시간1108.3초, 결과 패키지2,588,753,360bytes다. 15°의64→128동공 격자 비교에서 ghost electron-rate 상대L1 차이는 중심6.4–9.1%, outer-ne14.2–14.6%였다. 모든 비교가 데모5%조건을 넘었으므로 **수렴 미결정**이며, 현재 지도·수치를 정량 고스트 사양으로 사용할 수 없다. 기능 연결과 정량 정확도 확보는 구분한다.

## v2 준비 실행과 시연 순서

새 차트·선택 좌표의 준비 작업 `run-048e91d2ba0f4a88bc6c3896b5fd8f3c`는 완료됐다. 중심·고스트 peak·ring의 두 차트와 두 광량을 계산해 **12개 조건, 288장의 정수 RAW**를 저장했다. 계산시간은333.9초, 결과는536,231,488bytes이며225개 파일 해시를 검증했다. [준비된 암부 비교](http://127.0.0.1:8780/reference-ghost.html?run=run-048e91d2ba0f4a88bc6c3896b5fd8f3c&probe=probe-ring&chart=dark_contrast&angle=15&power=10)를 바로 열 수 있다.

검은 배경 ring 측정 영역의 광원 OFF 대비 평균 신호는1 W/m²에서7.123e,10 W/m²에서71.156e였다. 강한 peak 위치의 포화 영역은 미판정으로 남긴다. 암부 입력은 D65 기준 white의0.2–2%이므로 광원 OFF에서도 일부 낮은 대비 물체가 잡음에 묻힌다. 물체·배경에 고스트가 다르게 더해지면 signed 대비와 CNR은 음수가 되거나 커질 수 있으며, 이를 가시성 개선으로 해석하지 않는다.

이번 위치의64→128동공 격자 비교도6.39–14.08% 차이로 **수렴 미결정**이다. 연결 기능과 저장 결과의 일치 검증은 통과했지만 정량 고스트 사양을 보증하지 않는다. 아래 순서로 시연한다.

표시 white만0.1로 바꾼 재처리 `run-bba6a8d8d8c54243b1554bf80d1bf7e0`도 완료됐으며,36개 RAW 묶음의 원본 해시가 모두 같고 신규 취득은0회다. v2는 브라우저의 정수형 JSON 숫자와 SDK의 소수형 숫자를 같은 물리 값으로 정규화해 `15`와 `15.0` 때문에 같은 광선 캐시를 다시 생성하지 않는다. 작업 취소는 JSON 요청을 보내는 공통 작업 경로를 사용한다.

1. `black`, 저장된 peak 또는 ring 위치에서 광원 OFF / 직접광＋고스트를 비교한다. 지도에서 고스트 위치와 native 사각형의 관계를 설명한다.
2. A를 직접광만으로 바꿔 광원 자체의 직접상과 2회 반사 기여를 분리한다. 정수 RAW / 고정 ISP를 바꾸고 같은 case·좌표·원본 배열 링크가 유지되는지 확인한다.
3. `dark_contrast`로 바꿔4개 암부 단계와12개 물체의 대비·signed CNR·시간 잡음 변화를 본다. 1·10 W/m²를 비교하되 포화·무효 항목은 미판정으로 남긴다.
4. 지도에서 다른 위치를 선택한다. 저장 결과가 없으면 비워진 native 화면과 선택 좌표를 확인한 뒤 명시적으로 RAW 계산을 제출한다. 완료 후 새 run의 실제 결과를 확인한다.
5. 표시 white 변경은 같은 RAW 재처리로 제출한다. 원본 hash와 취득 설정이 유지되는 별도 결과를 연다.

v2에서도 pupil refinement 결과를 함께 확인해야 한다. 차트·위치 선택 기능을 추가했다고 기존 수치 수렴 미결정이나 하우징·산란·회절 제외 조건이 해소된 것은 아니다.

지도 클릭으로 선택한 `(0, 0.022175)` mm의 별도 실행 `run-027be07b4076400797b6f23232b38e0f`도 완료했다. 검은 배경·10 W/m²에서 세 비교군24장의 native RAW를248.2초에 계산했으며, 화면의 좌표·case·RAW 해시가 일치함을 확인했다. 근거는 `outputs/reference-ghost-probes-20261012/clicked-probe-verification.json`과 `browser-verification.json`에 보존한다.
