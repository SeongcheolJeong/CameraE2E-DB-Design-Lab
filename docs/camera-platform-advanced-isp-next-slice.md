# 다음 ISP 확장: 보존 Native RAW의 shading·잡음·세부 보존 비교

2026-09-21 현재 소스를 확인한 **미구현 제안**입니다. 아래 v2 schema, stage API,
calibration import와 replay 흐름은 현재 제공 기능이나 완료된 실행 근거가 아닙니다.
제품 소스를 변경하거나 기존 RAW/ISP 결과를 새 검증으로 승계하지 않습니다.

첫 사례는 **“같은 원본 RAW의 주변부 shading을 보정하면서 잡음과 디테일 손실,
sharpen halo를 어느 범위까지 허용할 것인가?”**로 고정합니다. 첫 구현은 단일 stream의
보존 native RAW 재처리입니다. 공간 LSC와 noise-calibration 기반 RAW denoise,
선택적 bounded sharpen을 하나의 고정 v2 그래프로 비교합니다. 임의 ISP graph,
시간 denoise, 새 촬영이나 제조사 ISP 복제까지 한 번에 구현하지 않습니다.

이 범위는 광학·전기 계산을 다시 수행하지 않고 실제 보존 RAW의 처리 결과를 바꿉니다.
요구사항→고정 카메라/RAW/보정→ISP 후보→근거→설계 판단→다음 실측을 연결할 수 있어,
하드웨어 timing 단계 이름을 늘리는 것보다 먼저 완결할 가치가 있습니다.

## 현재 구현에서 출발할 경계

| 소스에서 확인한 현재 동작 | v2에서 필요한 결정 |
|---|---|
| [`isp.py`](../src/camera_platform/isp.py)의 v1은 `black_level → demosaic → white_balance → ccm → tone`의 고정 float64 graph입니다. | v1 profile·stage 순서·출력·재현 근거는 그대로 유지합니다. v2를 새로운 schema/backend로 선택합니다. |
| [`process_raw`](../src/camera_platform/isp.py#L296)는 integer RAW만 읽고 signed BLC·demosaic·WB·CCM 배열을 보존합니다. Tone 직전에만 `[0,1]`로 자릅니다. | v2에서도 새 전하·잡음·노출을 생성하거나 프레임 최댓값으로 정규화하지 않습니다. 새 stage는 부호와 별도 mask를 유지합니다. |
| [`imaging.submit_replay`](../src/camera_platform/imaging.py#L512)는 원본 RAW, format, geometry, acquisition 조건, 불변 ISP 모델과 실행 환경을 묶습니다. | 추가 calibration blob·stage 구현·원래 sensor/lens/모드 identity를 제출·Worker·cache에서 함께 결속해야 합니다. |
| [`replay_source`](../src/camera_platform/imaging.py#L382)는 현재 DCG paired run을 명시적으로 거부합니다. | paired 축을 frame 축으로 재해석해서 이 검사를 우회하지 않습니다. 경로 추출은 별도 제안 경계입니다. |
| [`HWIspStage`](../src/pyisetcam/hwisp.py#L51)는 domain/buffering/window/cycle latency를 기록합니다. [`hw_isp_simulate_frame`](../src/pyisetcam/hwisp.py#L907)의 픽셀은 `camera_compute` 또는 `camera_compute_temporal`에서 계산합니다. | `dpc`·`tnr` 등의 stage 이름이나 latency를 추가하는 것만으로 해당 영상 알고리즘이 구현되었다고 주장할 수 없습니다. 실제 v2 픽셀 처리와 HW timing 추정은 별도 backend입니다. |

기존 [`camera-platform-isp-replay.md`](camera-platform-isp-replay.md)의 v1 지원 형식과
제한은 유지합니다. v1이 허용하는 대표 정규화 raster를 실제 native LSC 좌표로
추정하지 않습니다. 첫 v2는 명시적인 native sensor/crop만 지원합니다.

## 최소한의 v2 graph와 모듈 계약

제안 profile은 `camera_isp_profile_v2`, backend는 `camera_platform_float_isp_v2`입니다.
아래 순서를 고정하고 새 세 stage에는 `bypass`를 명시합니다. 알 수 없는 stage,
순서 변경, 누락된 calibration은 오류이며 자동으로 bypass하지 않습니다.

```text
integer RAW
→ black_level
→ spatial_lsc       [bypass 또는 native_four_site_grid_v1]
→ raw_denoise       [bypass 또는 calibrated_same_site_filter_v1]
→ bilinear demosaic
→ fixed RGB WB
→ CCM
→ linear_sharpen    [bypass 또는 bounded_detail_v1]
→ declared tone clip/transfer
```

첫 slice에서 세 stage는 독립적으로 켜고 끌 수 있지만, 하나의 frozen graph 안에서만
조합합니다. Sharpen 기본값은 bypass입니다. 새 stage를 모두 bypass한 v2의 공통
중간 배열과 RGB가 v1과 일치해야 하고, **기존 v1 요청은 v2로 자동 이행하지 않습니다.**
프로필 schema가 다른 만큼 v2의 profile/job hash는 별개이며 v1 해시를 재사용하지 않습니다.

새 순수 stage의 제안 인터페이스는 `run_stage(buffer, config, context, calibration)`입니다.
공통 결과는 `{array, validity, diagnostics}`이고 전체 실행이 stage별 산출물을 저장합니다.

- `buffer`: float64 Bayer 또는 sensor-RGB, 단위·축·기준 처리 단계가 명시된 배열.
- `context`: 원본 RAW SHA, acquisition/frame ID와 순서, full native shape/crop origin,
  pixel pitch, RGGB site phase, frozen sensor/lens/readout identity.
- `calibration`: immutable record ID/hash와 검증한 numeric blob. 입력 array를 보고
  자동 추정한 값과 실측 calibration을 같은 이름으로 받지 않습니다.
- `validity`: 원본 endpoint/알려진 calibration 경계와 stage 의존 stencil의 mask.
  실제 센서 포화 truth나 보정으로 복구된 정보라는 뜻이 아닙니다.
- `diagnostics`: stage ID/version, 설정/hash, 적용·bypass 여부, mask/경계 정책,
  calibration 적용 범위와 미확정 항목. 모든 JSON은 finite입니다.

Native site 이름은 `[R,Gr,Gb,B]`와 원점 tile을 유지합니다. Demosaic 이전에 Gr/Gb를
합치지 않습니다. Demosaic 뒤의 3채널 sensor RGB와 raw-site 축은 서로 다른 타입이며
같은 `channel`이라는 이유로 자동 broadcast하지 않습니다.

## 세 stage의 구체적인 첫 구현

### 1. Native spatial LSC

원본 sensor row/column 좌표의 작은 직사각 grid와 4사이트별 **양의 보정 gain**을
사용합니다. 제안 축은 `[grid_row,grid_column,site]`, site 순서는 `[R,Gr,Gb,B]`입니다.
Grid node는 full sensor의 pixel-center 좌표를 명시하고, 처리 crop의 모든 좌표를
포함해야 합니다. Crop마다 grid를 정규화하거나 가장 가까운 값으로 domain 밖을
연장하지 않습니다. 첫 보간은 명시적 bilinear이고 node마다 임의 radial fit을 하지 않습니다.

보정은 BLC 뒤 signed Bayer 값에 해당 site gain을 한 번 곱합니다. 제안 bounds는
축당 2–65 nodes, gain 0.25–16이며 더 큰 보정은 새 지원 범위가 필요합니다. RAW code와
original photon/electron 배열은 바꾸지 않습니다. 기존 Lens RI·stop transmission·native
EQE가 이미 만든 shading을 **후단에서 보정**하는 것이며 광학 throughput을 되돌리거나
새 광자를 만드는 연산이 아닙니다. 같은 calibration/보정 identity의 gain을 Sensor
응답이나 ISP WB에도 다시 적용하면 중복 보정이므로 stage history에서 거부합니다.
서로 독립적인 보정에서 우연히 같은 숫자가 나온다는 이유로 거부하는 것은 아닙니다.

Calibration은 reference ROI와 그곳에서의 정규화 방식을 기록합니다. 사이트마다
reference를 따로 1로 둔 grid는 그 지점의 상대 색 균형을 유지하는 공간 보정일 뿐,
white balance나 절대 colorimetry 보정이 아닙니다. Lens/조리개/focus, sensor mode,
illumination 또는 spectrum 적용 범위, 온도와 source kind를 고정합니다. 현재 평가할
장면을 flat-field로 오인해 LSC를 스스로 추정하지 않습니다.

### 2. Calibration-guided same-site spatial denoise

첫 알고리즘은 Bayer 4 sublattice 각각의 **3×3 same-site 이웃**만 사용합니다. Native
격자에서는 중심으로부터 최대 2 pixel 떨어진 범위입니다. 서로 다른 CFA site나 다른
frame의 값을 섞지 않으며 motion history·TNR·AI 학습 모델은 없습니다.

Noise calibration은 BLC 이전/이후 기준을 고정한 DN-domain의 signal-dependent variance
hint로 입력합니다. 첫 제안은 각 사이트의 nonnegative slope/intercept와 정확한 gain,
온도, exposure/readout mode 적용 범위입니다. 보정 record에 DN, DN², normalization을
명시하고 실제 값이 적용되는 구간을 제한합니다. 이미 제거한 pedestal/dark를 다시 빼지
않으며, v1 BLC처럼 black만 빼는 기본 의미를 바꾸지 않습니다.

제안 알고리즘은 이 variance hint와 실제 이웃의 code 차이로 nonnegative range weight를
계산하고, 고정 spatial weight와 곱해 합이 1이 되도록 정규화합니다. `strength ∈ [0,1]`로
원본과 가중 평균을 섞습니다. 분모 floor는 단위가 있는 명시적 tuning parameter입니다.
센터가 유효하면 센터 weight를 유지하고, invalid 이웃은 제외합니다. 정상 이웃이 없는
invalid center는 원래 값을 유지하고 invalid로 표시하며 복원했다고 주장하지 않습니다.

LSC 전의 variance hint를 LSC 뒤에서 쓸 때는 **선언된 고정 gain의 제곱**을 적용하고
BLC normalization의 제곱으로 단위를 변환합니다. 이는 고정 gain 조건의 대수적
변환이며 calibration uncertainty를 포함하지 않습니다. 공간 간 noise independence도
별도 가정입니다. 데이터에 따라 가중치가 바뀌는 denoise 뒤의 실제 covariance를 단순
`sum(weight² × variance)`로 정확하게 전파했다고 보고하지 않습니다. 필터용 hint와
검증용 불확실도는 별도 필드이고 후자는 unknown으로 남습니다.

이 내용은 구현할 연산의 설계입니다. 특정 공개 알고리즘·표준·상용 ISP와 동등하거나,
모든 장면에서 SNR/디테일을 개선한다는 주장으로 이름을 사용하지 않습니다.

### 3. 선택적 bounded linear sharpen

첫 sharpen은 CCM 뒤·tone 앞의 float sensor RGB에서만 수행합니다. 고정 3×3 blur와
명시된 RGB detail guide로 고주파 차이를 얻고, 사용자가 고정한 threshold·amount·최대
추가량으로 detail을 제한합니다. 이 guide를 측정된 photopic luminance라고 부르지
않습니다. 제안 `amount`는 0–1이고 추가량 상한은 normalized linear RGB 단위입니다.

RGB 세 채널에 적용되는 규칙, signed 값 보존, invalid stencil의 bypass를 고정합니다.
추가량 제한은 연산의 bound이며 sharpen 후 전체 값을 `[0,1]`로 먼저 자르는 숨은 tone
연산을 넣지 않습니다. 최종 clipping은 기존처럼 tone 직전에 명시합니다.
Noise-aware RAW denoise 뒤의 정확한 covariance가 없으므로 sharpen threshold를
정확한 n-sigma 검정이라고 표시하지 않습니다. Sharpening 후 edge/SFR은 처리된 영상
지표이며 광학 Lens MTF로 다시 등록하지 않습니다.

## 경계·보정·자원 계약

반사 경계는 각 same-site plane과 RGB stage에서 명시적으로 적용합니다. Crop 밖의
실제 RAW는 없으므로 full-frame과 crop의 경계 결과를 같다고 주장하지 않습니다.
제안한 고정 stencil에서는 denoise 2 + demosaic 1 + sharpen 1의 합인 **최소 4 native
pixel halo를 제외한 interior**를 crop/full-frame 동등성 시험에 사용합니다. 알고리즘
footprint가 바뀌면 이 halo도 새 버전으로 계산해야 합니다.

보정 record에는 다음을 결속합니다.

- 원본 flat/dark 또는 독립 모델 산출물의 ID·blob SHA, 추정/실측 구분, 보정 도구·방법·버전.
- 측정 camera serial/lot과 instrument/calibration 식별자, 학습 및 holdout dataset 구분.
- 센서·Lens·readout mode·geometry·CFA·pixel pitch와 노출/gain/온도/광원 적용 범위.
- 원래 grid/variance coefficients, 단위·reference ROI·정규화·보간·유효 mask와 원본 파일.
- 불확실도와 미확정 값. 출처가 `measured`라는 label만으로 물리 보정 승인하지 않음.

첫 slice는 이미 만들어진 명시적 LSC grid와 noise calibration의 import·검증·사용부터
지원합니다. Calibration fitter, 여러 illuminant 보간, 자동 sensor mode 전환은 이후입니다.
합성 fixture로 구현을 검증할 수 있지만 그 결과를 실제 카메라 교정 자료로 표시하지 않습니다.

첫 예산은 기존 replay의 전체 frame-pixel 상한 안에서 시작합니다. Stage별 output,
float Bayer/RGB 중간 배열, validity 및 calibration interpolation 임시 배열을 합산하여
**배열별 128 MiB·working 추정 1 GiB**를 넘으면 제출 전에 거부합니다. 3×3 same-site
필터의 9개 이웃을 한꺼번에 전체 영상 copy로 쌓지 않고 bounded accumulation합니다.
단순 resize·crop 변경으로 초과 입력을 통과시키지 않습니다. CPU wall time을 측정하되
이를 HW cycle/latency 수치나 실시간 처리 보증으로 바꾸지 않습니다.

## API·UI·저장·재실행의 최소 수직 흐름

첫 v2는 **보존 native RAW replay backend**로만 선택합니다. 기존 Camera/Temporal/HDR/DCG
획득 Runtime에 새 ISP가 자동 연결되었다고 표시하지 않습니다. 획득 경로가 v1만
지원하면 Composer/제출 단계에서 해당 v2 조합을 설명과 함께 거부합니다.

1. Calibration 원본과 metadata를 immutable 자산으로 등록하고 v2 ISP profile에서
   정확한 revision을 선택합니다. v1 model/template의 기본값은 유지합니다.
2. 기존 replay 제출 경로에서 source RAW·frame/acquisition 조건과 v2 calibration의
   geometry·mode·domain을 비교합니다. 부족한 조건은 default로 채우지 않습니다.
3. 별도 Worker가 같은 검사를 다시 수행하고 RAW·보정 blob·모델 snapshot·profile과
   실행 코드/환경 hash를 확인한 뒤 처리합니다. Cache hit도 원본 blob의 현재 무결성을
   먼저 검사합니다. 입력 RAW checksum은 전후 불변이어야 합니다.
4. BLC/LSC/denoise/linear RGB/sharpen/tone의 stage output, mask, gain map, noise hint,
   ROI 진단을 NPY로 보존합니다. Preview만 남기지 않고 stage별 identity와 byte SHA를
   evidence ZIP에 포함합니다. 저장/재시작 후 같은 replay와 산출물을 열 수 있어야 합니다.
5. UI는 “카메라 재촬영”이 아닌 “같은 RAW의 처리 비교”로 표시합니다. 사례의 원본
   RAW·기준 profile·후보 profile을 고정하고 stage 토글·LSC calibration·denoise strength·
   sharpen amount/threshold를 일반 폼으로 제공합니다. 고급 JSON은 상세 화면에 둡니다.
6. 원본/기준/후보를 같은 frame·crop·ROI·display scale로 비교하고 중간 단계·mask와
   원본 download를 제공합니다. 설정 변경은 새 profile/replay를 만들며, 늦게 도착한
   결과가 새 선택을 덮지 않고 등록 오류에도 초안이 남아야 합니다.

원본 acquisition이 실측이라는 사실과 replay 알고리즘의 검증 상태는 구별합니다.
시뮬레이션과 일치하거나 색이 좋아 보인다는 이유로 physical validation 또는 release
approval을 부여하지 않습니다.

첫 slice는 전체 replay cache와 stage별 trace를 완결합니다. 이후 단계별 cache를
도입할 때는 직전 단계 출력 SHA·현재 설정·calibration SHA·geometry·구현/환경을 key에
포함합니다. CCM만 바꿨다면 그 이전 단계는 같은 근거를 재사용하고 그 이후만 새로
처리할 수 있지만, 재사용된 전단 출력을 새로운 광학·전기 실행으로 세지 않습니다.

## DCG는 별도 extraction 계약이 먼저 필요함

현재 `raw_groups[acquisition,readout,row,column]`를 `[frame,row,column]`로 flatten하는
변환은 허용하지 않습니다. 후속 extraction 자산은 원본 run/artifact SHA와
`acquisition_id/index`, `stream_id=HCG|LCG`, 원래 readout index, exact native geometry,
해당 경로 RAW format·calibration, capture-program ID/hash, shared-charge group과
clock/timing을 함께 보존해야 합니다. 추출된 array의 SHA와 원본의 선택된 slice byte가
정확히 같음을 검증하고 어떠한 gain 재적용·재양자화·dark 이중 보정도 하지 않습니다.

이 extraction이 구현되면 **선택한 단일 경로**만 v2 입력으로 사용할 수 있습니다.
HCG/LCG noise model은 서로 바꾸어 쓸 수 없고 두 경로 비교를 독립 촬영으로 세지
않습니다. DCG merged_rate/linear RGB는 integer RAW가 아니므로 이를 ADC 코드로
되돌려 v2에 넣지 않습니다. 필요하면 별도의 float-radiometric ISP 입력 schema로
설계해야 합니다. 첫 ISP slice에서는 현재의 DCG replay 거부를 유지합니다.

## 독립 수용 기준과 최적화 범위

| 수용 기준 | 반드시 남길 실제 근거 |
|---|---|
| v1 불변·v2 bypass 동등성 | 기존 v1 regression, 동일 RAW bytes·CFA 4위상·signed 중간값·RGB 비교. 새 profile hash를 과거 hash로 위장하지 않음 |
| LSC 좌표와 단일 적용 | 비대칭 4사이트 grid의 독립 scalar bilinear oracle, node/interior/crop 시험, 원본 RI/RAW 불변·LSC 2회 적용 거부 |
| Denoise 의미 | strength 0 불변, 상수·edge·impulse·서로 다른 Gr/Gb, noise calibration 변화에 따른 실제 출력 변화, invalid 이웃과 경계 독립 oracle |
| Noise와 디테일의 trade-off | 보정에 쓰지 않은 반복 flat/edge/texture RAW의 사전 고정 ROI·절차로 noise와 bias·detail attenuation을 각각 기록. 시간·공간 통계를 혼동하지 않음 |
| Sharpen 범위 | 부호·추가량 bound·threshold·bypass·halo/undershoot를 독립 식으로 확인. 처리된 SFR과 광학 MTF를 구분 |
| 무결성과 잘못된 입력 | RAW/보정 blob 변조, 다른 sensor/lens/mode/phase, out-of-domain, 잘못된 stage 순서·단위, 초과 자원은 submit/Worker/cache에서 거부 |
| 실제 수직 실행 | 격리 API→별도 Worker→출력/ZIP SHA→재시작 재개방, 실제 UI profile 저장·전환·stage 비교·오류 회복. Form unit test를 화면 관측으로 세지 않음 |

최적화도 이 사례의 작은 sweep으로 시작합니다. Calibration revision은 고정하고
`denoise strength × sharpen amount`의 유한 조합만 동일 RAW에 적용합니다. Flat의
공간 편차, holdout noise/bias, edge halo, texture 보존을 별도 축으로 비교하고 하나의
“품질 점수”로 자동 채택하지 않습니다. Baseline/candidate에 서로 다른 crop·tone 또는
측정 절차를 적용한 결과는 같은 요구사항 근거로 합치지 않습니다. 최적화에 쓴 장면은
독립 검증에서 제외하며 목표를 못 채운 영역을 다음 실측/보정 과제로 기록합니다.

일곱 모듈의 역할은 유지합니다. Registry는 RAW·보정·프로필·근거를, Factory는 보정
후보를, Composer는 형식/적용 범위를, Runtime은 replay를, Validation은 조건이 맞는
독립 잔차를, Experiment & Optimization은 bounded 비교를, Agent & Release Control은
같은 API의 재현 가능한 실행과 사례 판단을 담당합니다. 네 실행 방식도 유지하며
이 replay는 Response-DB 활용 흐름 안에 둡니다. Quick Estimate는 후보 비용/효과의
명시적 추정, Detailed Characterization은 후속 보정 특성화, Surrogate는 독립 학습·검증
자료가 준비된 뒤의 별도 기능으로 남깁니다.

첫 구현을 막는 핵심은 알고리즘 개수보다 **native 좌표에 맞는 calibration 근거,
RAW domain의 noise 의미, 비선형 처리 이후 불확실도와 경계 정책**입니다. 이 셋을
명시하지 못하면 새 stage 이름이나 좋은 미리보기만으로 완료 처리하지 않습니다.
