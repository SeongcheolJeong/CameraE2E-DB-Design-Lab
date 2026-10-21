# Calibration & Validation Lab

CameraE2E의 파일 기반 실측 보정 작업실이다. `/calibration-lab.html`에서 캠페인을 만들고
CSV/JSON/선형 RGGB NPY를 등록한 다음 파라미터 후보와 새 RAW·ISP 차트 계산을 생성한다.
원본 측정, 기준 프로파일, 보정 후보, 독립 검증, 적용 이력은 모두 별도로 보존한다.

## 사용 순서

1. **캠페인**: 카메라 serial, native shape/pitch, 고정 mode/gain/temperature/light/
   aperture/focus와 기준 프로파일을 지정한다. 화면에서 JSON 템플릿을 받을 수 있다.
2. **측정 등록**: 차트·데이터 역할과 metadata JSON, 원본 CSV 또는 NPY를 입력한다.
   CSV는 `frequency_cy_px[cy/px],mtf[1]`처럼 단위를 헤더에 쓰거나 `units`에 선언한다.
   원본 파일은 content-addressed blob으로 저장하며 분석 결과와 연결한다.
3. **보정·새 실행**: training 자료만 피팅에 사용한다. development 자료는 같은 후보의
   비교에만 사용한다. 새 후보로 실제 전기 모델을 실행해 native RAW와 고정 ISP 결과를 저장한다.
4. **결과 확인**: 실측/기준/후보 곡선, 잔차, 파라미터 변경, 수렴/식별 제약,
   새 RAW·ISP 탐침과 재측정 지표를 확인한다. 참고 영상은 실측 이미지의 복사본이 아니다.
5. **최종 독립 검증**: 후보를 고정한 뒤 별도 session/acquisition의 final_validation 자료로
   새 작업을 수행한다. 이 단계에는 optimizer를 호출하지 않는다.
6. **선택 적용**: 완료 후보를 명시적으로 적용하면 다음 Calibration Lab 작업의 기준으로
   선택할 수 있다. 이전 프로파일 또는 원래 기준으로 돌아갈 수 있다.

`합성 예제 실행`은 실제 Worker와 저장 경로를 사용하는 구현 검증이다. 실측 카메라 성능,
장비 인증 또는 물리 검증으로 표시하지 않는다. 장비 제어와 RAW packing/제조사 decoder는
이 버전에 포함되지 않는다.

## 차트별 계약

| 차트 | 관측값 | 추정·비교 범위 |
|---|---|---|
| dark | exposure_s, mean_dn, variance_dn2 | offset·노출 기울기, 조건부 dark current 및 시간 잡음 |
| ptc | mean_dn, variance_dn2, 선택적 exposure_s/clipped_fraction | 유효 DN/e와 조건부 read-noise; FWC는 별도 근거 없이 변경하지 않음 |
| shading | field_radius, relative_response | 합성 radial shading; 렌즈·조명·센서 응답을 분리하지 않음 |
| mtf | frequency_cy_px, mtf, field_xy, orientation_deg | 전체 곡선에 대한 조건별 유효 Gaussian PSF; 위상·고유 광학 PSF는 미식별 |
| geometry | object_xy, image_xy; multiview는 view_ids/native_shape | 알려진 이상 영상좌표 warp 또는 다중 자세 planar camera calibration |
| color | reference_rgb, measured_rgb | 동일 white point의 linear RGB 응답 행렬; QE 스펙트럼을 추정하지 않음 |
| tone | input_level, output_level | 양 끝점을 포함한 단조 piecewise-linear 응답 |
| texture | frequency_cy_px, response | 명시된 기준 전달함수와 비교; 임의 denoise 알고리즘 피팅 없음 |

실측 MTF의 추정 모델은 `centered_elliptical_gaussian_v1`이다. DB 광학 초기값은 별도로
`field_effective_psf_v1`에 원본 sampled kernel을 보존할 수 있다. 충분한 방향별 관측이 없으면
isotropic 가정을 명시한다. 방향별 자료가 충분하면 타원형 응답을 피팅한다. 일반적인
비대칭/다중 peak PSF·ghost는 이 모델로 식별하지 않으며 남은 곡선 잔차를 숨기지 않는다.
기존 radial MTF→PSF 역변환은 독립 진단으로 재사용하며, 그 결과가 피팅된 Gaussian과
같은 커널이라고 표현하지 않는다.

실측 MTF에는 `measurement_domain`이 필요하다. 현재 자동 보정 실행은 `camera_raw`
유효 공간응답 대체 경로다. 광학 bench의 `optical`, 처리된 `camera_isp` 자료는 보존할 수
있지만 이 피팅에 섞을 수 없다. Broadband RAW MTF를 특정 파장의 렌즈 PSF로 바꾸지 않는다.

유효 PSF가 선언한 lens blur·pixel aperture·charge diffusion 등의 공간 효과를 이 경로에서
다시 적용하지 않는다. DB 초기값의 charge diffusion은 미식별 zero 가정으로 구분한다.
전기 모델은 `electrical.capture_electrons`를 재사용하며
FWC와 DN/e가 독립이다. 전압 anchor가 없으면 ADC full-scale 1 V의 **정규화 등가 전압**
기준을 선언하고, 물리적인 V/e를 측정했다고 주장하지 않는다.

다중 자세 geometry CSV는 `analysis.geometry_mode="multiview_planar"`,
`analysis.native_shape=[height,width]`, `object_x[mm],object_y[mm],image_x[px],image_y[px],view_id[1]`
열을 사용한다. Object 좌표는 평면 target 좌표다. 평가 영상에서 target pose를 다시 추정한
경우 해당 재투영 오차가 **pose에 조건부**임을 표시한다. 단일 fiducial RAW 입력은 알려진
이상 영상좌표 warp에 해당하며 독립적인 camera matrix 추정으로 표시하지 않는다.

## RAW와 측정 조건

- NPY는 정수 unpacked linear RGGB만 지원한다. `[H,W]`, `[repeat,H,W]`,
  `[level,repeat,H,W]` 배열이며 pickle/object/float/암시적 resize를 거부한다.
- 원본 최대 64 MiB, 한 프레임 4,194,304 pixels, 총 256 frames다. 헤더에서 자원 크기를
  검사한 뒤 배열을 로드한다. 큰 취득은 반복/조건별 파일과 같은 역할의 acquisition 계보로 나눈다.
- `geometry.full_sensor_shape`와 `crop_origin`을 필수로 보존한다. 홀수 crop 위치에서도
  전체 센서 기준 CFA 위상을 유지한다.
- Dark/PTC는 최소 두 반복으로 픽셀별 시간 표본분산을 구한다. 한 장의 공간 분산을
  시간 잡음으로 사용하지 않는다. Clipping·명백한 비선형·누락 관측은 보존하되 피팅을 거부한다.
- RAW MTF는 선택 CFA sublattice의 pitch가 native pixel의 두 배라는 사실을 반영한다.
  분석 주파수의 의미와 지원 범위는 관측 결과에 남는다.
- 실제 취득 metadata는 명령값이 아닌 적용된 설정을 기록해야 한다. 이 importer가 장비에서
  실제 적용값이나 교정 문서를 인증하는 것은 아니다.

원본 RAW를 가져온 경우 같은 관측 연산자를 사용할 수 있는 차트는 새 RAW에서도 그 연산자를
사용한다. 외부 CSV의 측정 방법을 재현할 수 없으면 조건부 모델 곡선 비교로 표시한다.
분석식 곡선·커널 전달함수·새 RAW의 측정 결과는 서로 다른 항목이며, 참고 chart probe가
실측 chart의 정확한 pixel-aligned replay라고 주장하지 않는다.

색·계조의 `measurement_domain`은 `camera_raw` 또는 `camera_isp`로 구분한다.
RAW 파일은 `camera_raw`로 정규화하며, RAW에서 추출한 CSV도 해당 domain과
`raw_format`의 고정 코드 정규화를 선언해야 한다. RAW 관측은 `raw_response`의
유효 평균 광전자 응답에, 선형 ISP 출력 관측은 `isp` 응답에 연결한다. 이 RAW 응답은
CFA와 shot noise 이전의 조건부 평균 모델이며 물리적인 QE 또는 전자회로 비선형성을
독립 식별한 결과가 아니다. `reference_white_e`는 별도 측정이 없으면 고정 가정이다.

## 버전·데이터 독립성

- Campaign, Measurement, Profile, Activation은 기존 SQLite registry의 불변 record다.
  별도 데이터베이스를 만들지 않는다. 원본과 결과 파일은 기존 blob/artifact ownership을 사용한다.
- session/acquisition/group/parent, RAW frame hash, 원본 bytes 및 관측 내용 hash를 보존한다.
  일치하는 hash 또는 선언된 취득 계보가 있는 자료는 다른 역할로 바꿀 수 없다.
  crop·재인코딩 자료에는 원래 acquisition/group/parent를 유지해야 한다. 사용자가 계보를
  누락하거나 변경한 서로 다른 crop까지 픽셀만으로 동일 취득이라고 인증하지는 않는다.
- 보정 후보의 development lineage는 부모 프로파일에서 상속한다. 별도 검증에 사용한
  acquisition은 다른 후보의 새 독립 검증으로 재사용할 수 없다.
- 명시적인 실행은 새 run ID를 만든다. 과거 완료 결과를 새 실행으로 표시하지 않는다.
  제출 시 프로파일·측정 snapshot·실행 코드/환경을 고정하며 실행 전에 다시 확인한다.
- 적용은 완료된 후보에만 허용한다. 취소·실패·수렴하지 않은 후보는 적용할 수 없다.
  적용 이력은 append-only이며, 원래 모델이나 특허·센서 DB를 덮어쓰지 않는다.

Threshold는 캠페인의 `thresholds`에 metric ID 또는 chart 이름별 최대 RMSE로 저장한다.
단위는 비교 metric과 같다. Threshold가 없으면 `inconclusive`이며, 평균 종합 점수로 다른
차트의 실패를 덮지 않는다. 알려진 측정 불확도를 입력해도 예측 불확도와 분석 방법 동등성이
자동으로 확보되지는 않는다. 따라서 이 Lab의 수치 허용오차 통과와 기존 플랫폼의 물리 검증
gate를 구분하고, 물리 검증은 `not_established`로 유지한다.

## DB Design Lab 연결

DB Design Lab의 보정 링크는 선택한 run/candidate를 전달한다. 서버는 완료된 DB 실행의
candidate와 hash를 확인하여 `source_context`로 보존한다. 같은 출처의 기존 캠페인을 다시
열 수 있다. 광학 모델은 아래의 명시적 변환 동작으로 새 캠페인의 시작 프로파일에 가져온다.

광량·노출 조건별 실행에서는 `condition_id`도 출처에 포함한다. 조건을 생략한 채 기본 조건의
후보를 대신 사용하지 않는다. 화면에서 캠페인을 바꾸면 선택된 캠페인의 출처가 표시와 복귀
링크의 기준이 된다.

### DB 전기 파라미터 연결

**DB 전기 기준 가져오기**는 완료 후보의 저장 파일을 hash로 확인하고 불변 bridge record로
보존한다. 현재 지원 범위는 선형 readout의 DN/e, black level, read noise, dark current다.
기준 full-well/bit-depth, native pitch와 RGGB는 유지하며, gain·temperature는 고정 조건이다.
원본의 가정·proxy 분류도 유지한다. DB 모델에서 복사했다고 실측값으로 승격하지 않는다.

DN/e는 다음 식으로 저장된 전기 모델에서 구한다.

```text
DN/e = conversion_gain_v_per_e × analog_gain
       × (2**bit_depth − 1) / adc.full_scale_v × digital_gain
```

Full-well을 바꿔 DN/e를 맞추지 않는다. Reference ISP도 명시적인 DN/e를 사용하여 RAW를
전자 수로 환산하고 noise hint를 계산한다. 이 필드가 없는 기존 실행은 기존 변환을 유지한다.
비지원 FPN, post-gain noise 또는 digital gain 모델은 정확한 전기 기준으로 변환하지 않는다.

DB의 256×256 영상은 각 field의 독립적인 가상 bench ROI이며 제품 센서 전체 해상도가 아니다.
Bridge가 물리 카메라의 full sensor shape나 focus distance를 추정하지 않는다. 실제 측정
캠페인에서는 카메라 식별자·전체 해상도·측정 조건을 명시해야 한다. 광학 자동 변환 전의 blur와
geometry, shading, RGB response, ISP 기본값은 작성된 가정이며 DB에서 추출한 값이 아니다.

### DB 광학 모델 자동 변환

**DB 광학 모델 자동 변환**은 선택한 완료 후보의 spectral PSF NPZ와 pupil/QE 응답을 읽어
새 캠페인 초안의 `blur`를 채운다. 조건별 실행이 NPZ를 복제하지 않은 경우에는
`source_capture`에 고정된 원본 run의 manifest hash, 광학 revision, NPZ hash를 모두 확인한다.
원본 바이트, 파장 가중치, 입력 프로파일, 변환 결과와 MTF 오차 보고서는 불변 record로 저장한다.

현재 지원하는 전송 모델은 Reference DB의 2.9 µm RGGB, exit-pupil diffraction,
450–650 nm D65 조건이다. 원본과 다른 pixel pitch를 임의로 재표본화하지 않는다.
화면에서는 먼저 **DB 전기 기준 가져오기**로 pitch·CFA를 맞출 수 있다.
저장된 source XYZ와 4개 CFA site의 uniform photocharge에 대한 일치 검사를 거친 뒤,
파장별 photon spectrum × pupil irradiance × absolute QE를 사용하여 PSF를 합성한다.

원본 PSF가 긴 꼬리와 좁은 중심부를 가지면 Gaussian 하나의 근사 오차가 크다. 따라서 실제
전송에는 `field_effective_psf_v1`의 `sampled_native_kernel_v1` node를 사용한다.
렌더러는 차트의 native source cell center를 샘플링한 뒤, pixel aperture가 이미 포함된
커널을 한 번 적용한다. 저장한 커널의 0–0.5 cycles/native pixel MTF를 다시 계산해 원본
broadband kernel과 비교한다. Gaussian 근사 곡선·오차는 별도 진단으로만 남긴다.

화면의 **광학 변환 범위** 기본값은 **중심 + 보존된 주변 시야 · DB 기준 좌표**다.
이 선택은 API에 `field_mapping_mode: "reference_model"`을 보내고, 원본에 실제 보존된
field만 연결한다. 현재 예제 원본은 `center`와 `outer-ne` 두 시야다. 다른 방향의 대칭
node를 만들거나, 존재하지 않는 field로 보간·외삽하지 않는다. 프로파일의
`blur.field_coordinate_domain`은 `reference_lens_object_direction`이며, DB 렌즈의
object direction을 뜻한다. 이를 실제 센서 위치나 센서 전체의 보정으로 해석하면 안 된다.

**중심만**을 선택하면 이전처럼 중심만 적용하고 주변 field는 진단으로 남긴다. API에서
mode와 mapping을 모두 생략한 경우도 이 동작을 유지한다. 실제 대응이 알려진 경우에는
**실제 센서 좌표 직접 지정**에서 `source_field_id`와 목표 `field_xy` 쌍을 입력한다.
이는 `field_mapping` 배열만 제출하고 `sensor_normalized` 좌표계를 사용한다.
원본 시야로 매핑 양식을 만들면 목표 좌표는 `null`이며 사용자가 직접 채워야 한다.
두 방식은 함께 제출할 수 없다. 범위나 대응을 편집하면 이전 초안의 광학 연결을 다시
변환하기 전까지 새 캠페인 등록을 막는다. 기존 캠페인은 변경하지 않는다.

변환 표는 원본 좌표, 대상 좌표계·좌표, 새 초안 반영 또는 캠페인 고정 여부를 표시한다.
MTF·texture 등록 화면의 **측정할 모델 시야**에서 실제 연결된 노드를 선택할 수 있다.
CSV는 `observations: {}`를 유지하고 `analysis.field_xy`와
`analysis.field_coordinate_domain`으로 위치를 선언한다. 직접 입력한 JSON 곡선에는
같은 키를 `observations`에 넣는다. 화면은 누락된 좌표만 명시적 analysis 선택에서 채운다.
저장된 데이터와 곡선·영상에는 시야 좌표와 방향을 함께 표시한다.

DB 기준 좌표 프로파일에는 동일 좌표계를 명시한 외부·모델 CSV/JSON 곡선만 사용할 수
있다. NPY RAW 분석은 실제 센서 crop 위치를 계산하므로 `sensor_normalized`에 한정한다.
물리 센서에서 DB 렌즈 방향으로의 대응을 알 수 없는 RAW를 reference 좌표로 바꿔
등록하지 않으며, 이 조합은 UI와 서버에서 거부한다.
현재 프로파일은 Gr 조건의 커널을 RGB에 공통 적용하므로 다른 CFA channel의 차이도 보고한다.
Charge diffusion은 원본에서 식별하지 않았으므로 `zero_not_identified` 가정으로 표시한다.

변환을 새 캠페인에 저장하면 `db_optical_profile_id`가 출처·정확한 blur·조건과 함께 고정된다.
기존 캠페인이나 활성 프로파일은 자동 변경하지 않는다. 후속 MTF 측정 fitting은 현재의
Gaussian 후보 모델을 사용하며, fitting한 field만 교체하고 나머지 원본 커널은 보존한다.
이 전송의 수치 일치는 실측 카메라 일치나 고유한 물리 PSF 식별을 뜻하지 않는다.

프로파일을 선택 적용한 뒤 **전기 보정값으로 DB RAW·ISP 재계산**을 실행할 수 있다.
선택 범위는 `electronics_only`로 고정한다. 기준과 보정 모델은 원본 DB 후보의 동일한
`mean-electron-rate.npy`를 사용하고, 각각 새 shot/dark/read noise와 clipping·ADC를 거친다.
이 입력에는 광학 PSF와 pixel/QE 응답이 이미 반영되어 있으므로 추가 blur, QE, pixel-area
계수를 적용하지 않는다. 기존 DB CCM과 ISP 설정도 고정한다.

새 실행은 적용한 4개 전기값, 이전·새 RAW/ISP 및 지표, 원본 hash와 프로파일 revision을
별도로 저장한다. 프로파일에서 달라진 PSF·shading·geometry·색·계조 값은 전송하지 않았음을
결과에 표시한다. 이런 값까지 원래 DB optical/spectral 실행기로 전달하려면 별도 모델 변환과
근사 오차 검증이 필요하다. 재계산 결과는 물리 검증이나 제품 성능 인증을 의미하지 않는다.

## API / SDK

모든 경로는 `/api/v1/calibration-lab` 아래에 있으며 기존 local token·origin 정책을 따른다.

| 작업 | API |
|---|---|
| 템플릿·차트 규격 | `GET /template` |
| 캠페인 생성·조회 | `POST /campaigns`, `GET /campaigns`, `GET /campaigns/{id}` |
| 관측 JSON / 원본 파일 | `POST /measurements`, `POST /measurements/import` (multipart metadata + file) |
| 후보·새 계산 | `POST /studies`, `GET /studies/{id}`, `POST /studies/{id}/cancel` |
| 고정 후보 독립 검증 | `POST /studies/{id}/validate` |
| 후보 적용·복귀 | `POST /studies/{id}/apply`, `POST /campaigns/{id}/activate` |
| 원본 포함 결과 묶음 | `GET /studies/{id}/bundle` |
| DB 전기 기준 보존·조회 | `POST /db-bridges`, `GET /db-bridges/{id}` |
| DB 광학 프로파일 자동 변환·조회 | `POST /db-optical-profiles`, `GET /db-optical-profiles/{id}` |
| 광학 변환 보고서·커널 | `GET /db-optical-profiles/{id}/files/{path}` |
| 적용 프로파일의 DB 전기 재계산 | `POST /db-replays`, `GET /db-replays/{id}` |
| 재계산 취소·결과 파일 | `POST /db-replays/{id}/cancel`, `GET /db-replays/{id}/files/{path}` |
| 합성 구현 검증 | `POST /demo` |

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=120) as client:
    template = client.calibration_lab_template()
    campaign = client.create_calibration_campaign(template["campaign_template"])
    # Fill actual metadata, measured source and instrument/reference evidence.
    metadata = template["measurement_templates"]["mtf"]
    metadata["campaign_id"] = campaign["id"]
    observation = client.import_calibration_measurement(metadata, "mtf.csv")
    job = client.submit_calibration_study({
        "campaign_id": campaign["id"], "measurement_ids": [observation["id"]],
        "base_profile_id": "baseline", "name": "MTF calibration", "seed": 42,
        "max_iterations": 100,
    })
    result = client.wait(job["id"], timeout=1860, expected_kind="calibration_lab")
    print(result["status"])
```

`base_profile_id="baseline"` explicitly chooses the original profile; a saved profile ID chooses
that revision; null/omission resolves the campaign's active revision at submission time.

## Verification

```sh
.venv-platform/bin/python -m pytest tests/platform/test_calibration_lab_contracts.py \
  tests/platform/test_calibration_lab_engine.py tests/platform/test_calibration_lab_api.py -q
node --test tests/platform/browser/calibration-lab.cjs
.venv-platform/bin/python tools/verify_calibration_lab.py \
  --base-url http://127.0.0.1:8780 \
  --output outputs/calibration-lab-verification/live-verification.json
```

Import, numerical recovery, actual Worker calls, fresh outputs, hash-bound downloads, independent
dataset admission, cancellation, manual apply and rollback are tested separately. Browser VM tests
are UI logic tests; actual browser/HTTP execution is recorded separately when performed.
The live verifier creates a new synthetic campaign, checks downloaded output hashes and the ZIP,
executes frozen-candidate validation, and restores that campaign's original profile after testing
apply. It does not activate a profile in an existing camera campaign.

The DB electrical bridge can be checked against a completed DB source, including a
non-default lighting/exposure condition:

```sh
.venv-platform/bin/python tools/verify_calibration_db_bridge.py \
  --base-url http://127.0.0.1:8780 \
  --source-run <completed-db-run-id> --candidate candidate-03 \
  --condition condition-006 \
  --output outputs/calibration-db-verification/live-verification.json
```

Omit `--condition` for a source without a condition grid. This verifier preserves the
selected DB input bytes, creates a separate synthetic Dark/PTC campaign, recovers
four electrical parameters, validates independent observations, and generates paired
baseline/calibrated DB RAW and ISP outputs. It checks output hashes and unchanged
source identity, then restores the verification campaign's baseline. The report is
implementation evidence; it does not establish physical camera validation.

Optical conversion and its fresh chart/fitting workflow can be checked separately:

```sh
.venv-platform/bin/python tools/verify_calibration_db_optics.py \
  --base-url http://127.0.0.1:8780 \
  --source-run <completed-db-run-id> --candidate candidate-03 \
  --condition condition-006 \
  --output outputs/calibration-optical-verification/live-verification.json
```

This verifier retains the original numerical optics, initializes a new campaign, fits synthetic
MTF observations, generates new baseline/candidate RAW and ISP probes, checks independent
observations and output hashes, and restores the verification campaign's baseline.

## 작업별 표준 최종 보고서

보정 계산, 그 후보의 완료된 독립 검증, 저장된 프로파일을 명시적으로 선택한 뒤
**표준 최종 보고서 발행**을 누른다. 기존 실행별 원본 보고서와 별도로 한 calibration 작업의
결론을 `camera_calibration_completion_report_v1` 형식으로 묶는다. 보고서는 불변 revision이며
활성 프로파일 변경이나 rollback 이후에도 같은 파라미터와 판정을 유지한다.

- `report.html`: 서버 연결 없이 열 수 있는 한국어 보고서. 측정 출처·조건, 변경 전후 파라미터,
  차트별 곡선·오차, 독립 검증, 저장 프로파일, 적용 범위와 한계를 포함한다.
- `report.json`: 같은 고정 보고서의 구조화 데이터와 출처 hash.
- `final-profile.json`: 해당 보고서에 고정된 최종 파라미터.
- `parameters.csv`: 변경 전후 파라미터와 단위.
- 근거 ZIP: 위 파일, fit/validation의 원본 작업과 결과, 측정 metadata와 원본 파일.

발행은 `POST /api/v1/calibration-lab/completion-reports`에 `study_id`, `validation_id`,
`profile_id`를 전달한다. 부모 실행·캠페인·후보 및 프로파일 hash가 다르면 거부한다.
동일한 세 ID와 보고서 형식 버전은 같은 보고서 record를 반환한다. 실패 또는 미평가 결과는
보고서에서 그대로 표시한다. 보고서 발행이 독립 검증 결과를 PASS로 바꾸지 않는다.

### 합성 RAW 이미지로 전체 과정 재현

```sh
PYTHONPATH=src .venv-platform/bin/python tools/run_calibration_raw_demo.py \
  --base-url http://127.0.0.1:8780 \
  --output outputs/calibration-raw-completion-unique-run \
  --seed 1729
```

이 실행기는 별도 합성 캠페인에 정수 RGGB NPY 이미지를 생성·등록하고, 정규 RAW 분석기로
차트 관측값을 추출한다. 진짜 파라미터는 별도의 평가용 파일로 보존하며 fitting 요청에 넣지
않는다. Training과 final_validation은 서로 다른 session/acquisition/seed를 사용한다.
독립 수치 기준을 통과한 경우에만 해당 새 캠페인의 후보를 저장·활성화하고 표준 보고서와
근거 ZIP을 출력한다. 기존 카메라 캠페인이나 DB 원본에는 적용하지 않는다.

합성 RAW는 끝까지 `source_kind=synthetic`이다. 같은 모델 계열에서 생성·추정한 구현 확인이며,
실물 카메라 성능 검증은 `not_established`다. Texture는 비교 전용이고, RAW MTF는 등방성
Gaussian 범위다. 기준 pedestal·광량 scale·색/톤 정규화 등 고정 입력과 식별 한계는 생성
protocol 및 보고서에 함께 남긴다.

같은 registry에서 반복할 때는 새로운 seed를 사용한다. `--seed`를 생략하면 새 무작위 seed를
생성한다. 동일한 seed의 원본을 다른 폴더나 캠페인에 복사해도 독립 검증 자료로 재사용할 수
없다. 중간 실패는 `execution.json`에 이미 생성한 ID를 남기므로 기존 결과를 확인한 뒤
이어서 처리한다. 완료 결과를 재출력할 때는 새 피팅 대신 보존 report ID를 사용한다.

## 분리된 분광 QE · 맥베스 RAW · ISP CCM 작업

`/calibration-color.html`은 Calibration Lab에서 연결되는 별도 색 보정 작업이다.
기존 `raw_response.color_matrix`는 기준 색에서 RAW 평균 응답으로 가는 유효 모델이며,
QE를 측정하거나 camera RGB를 목표 색으로 바꾸는 ISP CCM을 추정한 결과가 아니다.
이전 결과는 보존하고 새로운 작업에서 세 단계와 판정을 분리한다.

1. **파장별 광원 시험 → QE 프로파일**: dark를 뺀 DN을 고정 DN/e와 입사 광자 수로
   나눈다. 센서 표면 기준 절대 광량 교정, 파장·광원 대역폭, 노출, ADC 포화 여부,
   모드·게인·온도와 장비/교정/취득 식별자를 입력한다. CFA·microlens 포함 여부를
   명시하며 lens/filter를 중복 포함하지 않는다. 파장별 상대 광량만 알려진 경우
   채널별 상대 분광 응답으로 기록하고 절대 QE 또는 채널 간 감도비로 해석하지 않는다.
   광원 자체의 파장별 광량 보정이 없으면 상대 QE도 식별하지 않는다.
2. **고정 QE → 맥베스 RAW 검증**: sensor plane의 노출 적분 광자량 밀도
   `[photons/pixel/nm]`와 QE를 파장 적분하고 고정 전기적 계수로 RAW DN을 예측한다.
   교정 데이터와 별도 취득한 패치 평균 R/G/B, 비율, 잔차, 채널별 RMSE를 비교한다.
   광량·gain·WB·CCM을 추가 피팅하지 않는다. 입력 스펙트럼은 QE 대역 전체를 포함해야
   하며 외삽하지 않는다. 상대 QE로 절대 RAW DN을 예측하는 경로는 미평가 처리한다.
3. **고정 선형 camera RGB → ISP CCM**: `corrected = (camera_rgb * WB * exposure_scale)
   @ matrix` 행벡터 방향으로 3×3 행렬을 추정한다. WB·노출·RAW 정규화·목표 색 공간과
   광원 SPD hash를 고정한다. D65 XYZ/linear sRGB에는 조건부 ΔE00, 실측 카메라 선형
   출력 재현에는 해당 공간의 RMSE를 제공한다. gamma/tone을 포함한 비선형 영상은
   이 CCM 피팅의 입력으로 허용하지 않는다. 새 취득에 고정 행렬을 적용하여 검증한다.

공통 입력은 `camera_spectral_color_task_v1` JSON이며 `/api/v1/calibration-lab/color-template`
에서 작은 합성 예제를 받을 수 있다. QE 시험 표는 `qe_scan`, RAW 비교 조건은
`raw_validation`, CCM 데이터는 `ccm_training`과 `ccm_validation`으로 분리된다.
선택적 `raw_originals.training/validation`은 base64 NPY, RGGB, bit_depth, crop_origin,
네이티브 ROI `[top,left,height,width]`를 포함한다. 원본을 입력할 때 관측 배열은 비워야
하며 서버가 반복 프레임·CFA별 평균을 추출한다. G는 두 green site 평균이다.
프레임 공유·관측 재포장·학습/검증 취득 중복·다른 후보의 holdout 재사용을 거부한다.

`POST /api/v1/calibration-lab/color-calibrations`는 불변 작업과 보고서를 함께 저장한다.
같은 입력·코드의 재요청은 같은 ID를 반환한다. 완료 보고서는 QE 곡선, RAW 잔차,
CCM 전후 결과, 최종 두 프로파일, 고정 조건·출처·원본·코드 hash를 포함한다.
상대 QE, RAW 실패, CCM 실패를 각각 표시하며 CCM 성공으로 RAW 실패를 덮지 않는다.
QE·RAW의 CCM 전후 hash가 일치함을 확인한다. 프로파일은 이 분광 패치 모델 및 CCM
작업에 사용되며 기존 DB 실행이나 기존 차트 캠페인 활성 프로파일을 자동 변경하지 않는다.

```sh
PYTHONPATH=src .venv-platform/bin/python tools/run_calibration_color_demo.py \
  --base-url http://127.0.0.1:8780 \
  --output outputs/calibration-qe-ccm-new-run
```

이 실행기는 ISETCam의 Macbeth 24패치 분광 반사율과 D65의 380–780 nm 범위로
서로 다른 반복 Bayer RAW를 생성한다. 파장 스캔은 합성 광원 시험표이고 CCM 목표는
합성 카메라의 선형 ISP 출력이다. 물리적인 센서 QE·광학·장비·실물 색 정확도의 검증이
아니며 UV/NIR이나 다른 광원으로의 일반화도 주장하지 않는다. 원본, 표준 HTML/JSON,
`qe-profile.json`, `ccm-profile.json`, ZIP과 실행 readback을 출력한다.

## QE·CCM 프로파일을 DB 활성 모델에 적용

`/calibration-db-color.html`은 보존된 DB 후보·조건과 색 보정 작업을 연결한다.
모델 생성, 활성화, 재실행은 각각 명시적인 작업이다. 새 불변 모델 버전을 만든 다음
동일 원본·대상 카메라 범위의 활성 포인터를 교체한다. 제출한 실행은 그 모델과 활성화
기록을 고정하므로 이후 활성 모델이 바뀌어도 실행 내용은 바뀌지 않는다. 기존 DB 원본,
기존 실행, 다른 후보의 설정은 보존된다. DB Design Lab에서 활성 버전과 작업 링크를
확인할 수 있다. 이 활성 선택은 새 색 재실행 경로에 적용되며 기존 모든 DB 실행 레시피를
일괄 변경하는 전역 설정이 아니다.

현재 지원 범위는 `synthetic_model_transfer`이다. 합성 보정 결과는 새 합성 카메라 대상에
적용하며 실물 센서가 교정되었다고 기록하지 않는다. 실측 카메라로의 적용에는 같은
카메라·모드·측정 조건을 증명하는 별도 연결 계약이 필요하다.

QE는 기존 전자 발생률에 곱하지 않는다. 보존된 광학 입력에서 광자 전달을 다시 계산하고
센서 표면의 절대 QE를 교체하여 새 RAW를 만든다. 기존 전기적 gain·black·noise·ADC는
그대로 사용한다. RGB QE는 RGGB의 두 green site에 동일하게 적용하고 입사각 독립성을
명시한다. 지원 파장 범위 안에서만 보간하며 외삽하지 않는다. CCM은
`electrons @ diag(WB * exposure_scale / reference_white_e) @ matrix`를 적용한다.
기존 ISP 색 변환을 교체하므로 WB·CCM을 중복 적용하지 않는다.

보고서는 중심과 주변부의 네 결과를 보존한다.

| 결과 | RAW | ISP 색 변환 |
| --- | --- | --- |
| DB baseline | 기존 QE로 새로 생성 | 기존 DB ISP |
| QE only | 갱신 QE로 새로 생성 | 기존 DB ISP |
| CCM baseline | QE only와 동일 RAW | 프로파일의 기준 CCM |
| QE + fitted CCM | QE only와 동일 RAW | 보정 CCM |

RAW의 QE 변화, 같은 DB ISP 공간에서의 QE 변화, 같은 camera-linear 공간에서의 CCM
변화를 분리한다. 출력 공간이 다른 영상을 정확도 수치로 직접 비교하지 않는다. RAW·ISP
미리보기, 24패치 비교, 적용된 파라미터, 원본·코드·파일 hash를 HTML/JSON과 ZIP으로
출력한다. 이 DB 재실행의 전후 변화량은 독립 실측 오차나 합격 판정이 아니다. 이전 보정
작업의 holdout 검증은 해당 조건에 한정된다.

주요 API는 `/api/v1/db-design/color-models`, `/{model_id}/activate`,
`/api/v1/db-design/active-color-models`, `/api/v1/db-design/color-replays`이다.
활성화 요청은 `expected_active_model_id`를 보내 다른 변경을 덮어쓰지 않는다.
완료 실행의 `/files/report.html`과 `/bundle`로 표준 보고서와 근거 파일을 받는다.

```sh
PYTHONPATH=src .venv-platform/bin/python tools/run_calibration_db_color_demo.py \
  --base-url http://127.0.0.1:8780 \
  --output outputs/calibration-db-color-new-run
```

실행기는 모델·활성화·실행 ID를 즉시 보존하고 다운로드 및 ZIP의 모든 파일 hash를
검증한다. 같은 폴더에서 다시 실행하면 이미 제출한 실행을 이어서 확인한다.

### 동일 DB 조건의 새 합성 Macbeth holdout 비교 보고서

`tools/run_calibration_db_holdout.py`는 보존된 DB 색 재실행을 입력으로 받아, 기존
합성 카메라 정의의 QE·CCM과 별도 seed에서 새로운 가상 관측 A/B를 생성한다.
이미 학습한 QE·CCM을 고정 적용하며 새 관측으로 재피팅하거나 후보를 선택하지 않는다.
현재 예제는 Canon p0248 × IMX462 구조 모델, D65 1000 cd/m²·1 ms·gain 1,
중심/주변 우상 256×256·8프레임·450–650 nm 조건을 대상으로 한다.

가상 관측은 별도 NumPy Poisson·read noise·ADC 구현으로 생성하고, 보정 전후 예측은
기존 전기 시뮬레이터로 생성한다. 두 관측 취득과 두 예측을 합쳐 RAW 64장을 보존한다.
공통 광학 연산자와 ISP 계열을 사용하는 합성 검증이며 물리적 독립 검증은 아니다.

기존 DB ISP와 camera-linear CCM의 색 공간이 다르므로, 보정 전 ISP도 공통 camera RGB
출력 공간의 기준 CCM으로 처리한다. 보정 후에는 추정 CCM을 적용한다. RAW는 DN,
ISP는 clipping/gamma 이전의 선형 RGB를 사용해 24패치 오차·감소율을 산출한다.
별도 관측 A/B의 차이를 반복 취득에 따른 비교 기준으로 함께 표시한다.

```sh
PYTHONPATH=src MPLCONFIGDIR=/private/tmp/camerae2e-mpl-holdout \
  .venv-platform/bin/python tools/run_calibration_db_holdout.py \
  --replay run-ea76c588911140edabbe3a2528d7fbdf \
  --output outputs/calibration-db-holdout-new-run
```

출력은 이미지가 포함된 독립 HTML, 전체 수치 JSON, 원본 RAW·선형 ISP·고정 프로파일·
프로토콜과 SHA manifest를 담은 ZIP이다. 기존 DB 모델과 활성 포인터는 갱신하지 않는다.
출력 폴더가 이미 존재하면 덮어쓰기를 거부한다. 같은 seed의 재생성은 새로운 독립 취득으로
간주하지 않는다. 이 CLI는 별도 분석 보고서이며 서버의 보존 실행을 추가하지 않는다.

### Calibration Lab의 보정 후 자동 DB 비교

DB Design Lab에서 원본 실행·후보·조건을 선택해 Calibration Lab → QE·RAW·CCM
작업실로 이동하면 동일한 DB 조건이 전달된다. **DB 적용·가상 실측 비교·오차 보고서
자동 생성**을 선택하고 보정을 실행하면 아래 과정이 한 번에 이어진다.

1. 파장별 QE 추정, 학습 RAW 및 별도 검증 RAW 평가, ISP CCM 추정 결과를 보존한다.
2. 명시한 `synthetic-db-…` 카메라에 새 불변 QE·CCM 모델을 생성한다. 이전 활성 모델을
   비교한 CAS 방식으로 활성화하며 동시 변경을 덮어쓰지 않는다.
3. 같은 DB 광학·광량·노출·읽기 조건으로 새 RAW·ISP를 계산하고, 알려진 원래 합성
   카메라 정의와 새로운 seed의 관측 A/B를 별도로 생성한다. 이 관측으로 재피팅하지 않는다.
4. 중심·주변 우상의 **보정 전 / 가상 실측 / 보정 후** 차트, RAW DN·선형 ISP의
   24패치 RMSE·감소율·반복 차이를 화면과 HTML·JSON·근거 ZIP으로 자동 보존한다.

화면의 저장된 보정 선택은 연결된 최신 비교 결과를 조회한다. 시야와 RAW/ISP 표시 변경,
결과 재조회는 새 계산이나 활성화를 제출하지 않는다. 기존 저장 보정에는 **저장된 보정으로
DB 자동 비교**, DB 모델 작업실에는 **모델 적용 → 자동 비교·보고서 생성**을 제공한다.
DB Design Lab의 활성 모델 카드에서도 정확히 같은 원본·후보·조건의 보고서에 연결된다.

API는 `POST /api/v1/calibration-lab/color-db-workflows`이며 `model`(name, source,
binding)과 `calibration` 입력 또는 `color_calibration_id`를 받는다. 반환된 보정·모델·활성화·
실행 ID를 각각 보존한다. 계산 실패나 CAS 충돌에도 이미 생성한 보정과 모델은 삭제하지 않는다.
브라우저 편집기는 `calibration_json` 문자열로 JSON 원문을 전달한다. 일반 색 보정 등록도
`{"input_json":"…"}`를 지원한다. 이 방식은 `380.0`을 `380`으로 다시 직렬화해 내부에
고정된 프로파일 SHA가 달라지는 문제를 방지하며, 기존 프로파일 해시 검사는 그대로 유지한다.
완료 결과는 기존 `/api/v1/db-design/color-replays/{run}` 안의 `summary.holdout`에
보존하며 다음 경로로 다운로드한다.

- `/api/v1/db-design/color-replays/{run}/files/holdout/report.html`
- `/api/v1/db-design/color-replays/{run}/files/holdout/report.json`
- `/api/v1/db-design/color-replays/{run}/files/holdout/evidence.zip`

현재 자동 가상 실측은 원래 Gaussian QE·CCM 정의와 입력이 일치하는 합성 예제에 한정한다.
일반 실측·수정된 기준 입력에는 `not_evaluated`와 독립 관측 필요 사유를 기록한다.
웹 예제는 매 조회 새 seed의 학습·검증 취득값을 제공한다. 고정 입력을 재사용하는 것은 새
독립 취득이 아니다. 합성 holdout은 공통 광학·ISP 모델을 사용하는 소프트웨어 검증이며,
실제 QE·각도 응답·제품 카메라의 독립 실측 검증을 대신하지 않는다.

## DB 기반 Dark·PTC / MTF·PSF 자동 비교

`/calibration-db-core.html`은 Calibration Lab 및 DB Design Lab에서 원본 실행·후보·조건을
이어받는 보정 작업 화면이다. 한 번의 명시적 실행으로 가상 학습 RAW → 기존 RAW 관측기 →
전기 파라미터 추정 → 시야별 PSF 추정 → 후보 고정 → 별도 취득 A/B와 보정 전후 재시뮬레이션
→ 표준 HTML/JSON/CSV 보고서 및 원본 RAW/선형 ISP/PSF/출처 ZIP을 생성한다.

- Dark/PTC: black level, DN/e, read noise(e), dark current(e/s)를 공유 전기 파라미터로 추정한다.
  Dark 노출과 PTC의 알려진 전자량은 통제된 스윕이다. 독립 전자량 기준을 모르는 실물 RAW에서
  이 데모의 절대 전자량을 추론했다고 해석할 수 없다.
- MTF/PSF: DB 중심·주변부의 실제 D65 450–650nm Gr sampled PSF를 보정 전 연산자로 사용한다.
  새 이상적 에지에 공간 응답을 한 번 적용한다. 각 시야는 별도 128×128 국소 가상 센서이며,
  DB 렌즈 방향을 실제 센서 좌표로 식별한 결과가 아니다. Gr PSF는 공유 RGB 유효 블러 프록시다.
- RAW의 두 방향 Gr SFR을 관측하고 등방성·영위상 Gaussian PSF를 추정한다. RAW/ISP SFR은
  공통 관측 주파수 대역(최대 0.25 cycles/native pixel), 커널 OTF는 별도 표와 그림으로 비교한다.
  MTF만으로 고유한 물리 PSF를 복원하거나 비대칭·위상을 식별한 결과가 아니다.
- ISP는 원본 DB Reference ISP 행렬·튜닝을 고정하고 각 정수 RAW 프레임에 적용한 뒤 시간 평균한다.
  QE/CCM, full well, PRNU/DSNU는 이 경로에서 추정하지 않는다.
- 합성 정답은 생성기·평가기에만 존재한다. 피팅에는 학습 RAW에서 추출한 관측만 전달한다.
  후보를 고정한 뒤 별도 난수 스트림의 관측 A/B 및 전후 RAW를 생성하며 재피팅하지 않는다.
  파라미터 정답 비교는 알려진 합성 사례에만 제공하고 실제 검증 판정은 차트 오차로 한다.

API는 `POST /api/v1/calibration-lab/core-workflows`에서 `source`,
`modules: ["electronics", "optics"]`, 선택적 `seed`, `apply`를 받는다. 저장된 결과 조회와
차트 선택은 읽기 전용이다. 실행은 원본 파일·소스 지문을 고정하고, 사전 정의한 합성 데모 오차
허용치를 통과할 때만 불변 `calibration_db_core_models`를 등록·활성화한다. 계산 중 활성 버전이
변하면 CAS 적용을 차단하며 보고서는 보존한다. 완료되지 않거나 실패한 작업의 활성 이벤트는
표시하지 않는다. `/api/v1/db-design/active-core-models`는 완료된 모델만 반환한다.

활성화 범위는 선택한 모듈과 DB 원본·후보·조건에 연결된 **합성 파생 국소 프로브 모델**이다.
원본 DB 레시피·기존 RAW·기존 실행을 변경하지 않으며 QE/CCM 모델과 암묵적으로 합성하지 않는다.
현재는 기존 2.9µm, 21-band, classic_v1, 8-frame, LSC-off 원본 전송의 중심·outer-ne를 지원한다.
실물 측정, 측정 장비 제어, 물리 검증은 이 자동 합성 데모의 완료 범위가 아니다.

## 통합 카메라 프로파일 · QE/CCM + Dark/PTC/MTF/PSF

`/calibration-unified.html`에서 동일 DB 원본·후보·조건 및 manifest/request/result/candidate
핀을 가진 완료된 QE·CCM 모델과 Dark·PTC·MTF·PSF 모델을 선택한다. 명시적 실행 한 번으로
불변 `camera_unified_calibration_profile_v1`을 생성하고 전체 파라미터를 적용한 새 RAW/ISP와
별도 가상 실측 A/B의 차트 오차, 단일 프로파일 JSON, 표준 보고서, CSV, ZIP을 출력한다.
이 작업은 부모 보정값을 결합하는 단계이며 검증 자료를 이용해 재피팅하지 않는다.

적용 순서는 **DB spectral DC × 절대 QE → 시야별 유효 PSF 한 번 → Dark/PTC readout → ISP**다.
원본 DB 공간 컨볼루션을 먼저 수행하지 않으며, 이미 블러된 저장 전자량에 새 PSF를 덧씌우지 않는다.
기존 수신 픽셀 개구가 포함된 PSF를 사용하므로 두 번째 픽셀 개구 적분도 없다. Dark/PTC는
알려진 전자량 주입으로 전기 응답을 분리한다. QE 검증은 알려진 센서 표면 광자량의 별도
파장별 RAW이며 Macbeth에서 QE를 추정하지 않는다. 이 데모에서 보정 전 수신 QE는 DB
pupil 평균 응답의 합성 기준이며 실물 normal-incidence QE 측정과 구분한다.

통합 프로파일은 camera ID, 부모 ID/hash, 원본 핀, 조건, 시야별 QE/PSF, 공통 electronics,
CCM/WB/reference electron normalization, 정확히 한 번 적용하는 electron-to-output 행렬,
고정 파라미터와 가정을 포함한다. **QE 스캔 당시 DN/e는 출처 정보로 보존하고, 실제 RAW/ISP
DN→전자 변환은 Dark/PTC에서 보정된 DN/e를 사용한다.** 원래 CCM과 WB를 뒤에 다시 적용하지
않는다. CCM 효과만 보는 비교는 전체 보정 후 동일 정수 RAW를 재사용하며 SHA를 보존한다.

`POST /api/v1/calibration-lab/unified-workflows`는 `source`, `color_model_id`, `core_model_id`,
선택적 `seed`, `target_camera_id`, `apply`를 받는다. 부모 원본 파일/프로파일과 코드 지문을
고정하고 기존 부모 취득 seed와 새 역할별 취득을 분리한다. 미리 선언한 오차 허용치를 통과할 때만
`calibration_unified_models` / `calibration_unified_activations`로 CAS 활성화하며, DB Design Lab의
`GET /api/v1/db-design/active-unified-models`에는 완료된 작업의 모델만 노출한다. 저장 결과
선택, 표시 단계 변경, 보고서 열기는 새 계산이나 적용을 만들지 않는다.

범위는 D65 450–650nm, 같은 gain/temperature, 두 개의 독립 128×128 국소 가상 센서이다.
PSF는 원래 Gr 특성화에 조건부인 공유 RGB 유효 블러이며 spectral PSF 재식별 또는 물리
전체 센서 매핑이 아니다. 통합 가상 정답은 기존에 명시한 두 합성 생성기의 결합으로 정의한다.
독립 취득 및 별도 readout 구현의 검증이지만 광학/ISP 모델 자체의 독립 물리 검증은 미확립이다.
원본 DB 레시피·기존 RAW·부모 보정 모델은 보존한다.

### Unified PSF numerical convergence (2026-10-18)

New unified runs automatically qualify the frozen fitted effective Gaussian PSF
before any independent noisy holdout acquisitions. The search is q=1,2,4,8
samples/native pixel × support=3,4,5,6 sigma. At each setting, central and outer-ne
128×128 local probes use 0°/90° edges at phases 0 and 0.25 native pixel. Sampling
always preserves the native center at n+0.5. Integer RAW is rendered with noise
disabled and passed through exactly the frozen electrical/ISP operators.

The reference must itself pass q8/6σ → q16/6σ (sampling) and q16/6σ → q16/8σ
(support). Each kernel/RAW/ISP curve must differ by at most 0.005 MTF absolute,
with MTF50 relative difference ≤0.01; missing/incompatible frequency coverage or
missing MTF50 fails. RAW Gr is tested through 0.25 cycles/native pixel, ISP green
and kernel through 0.5. A conservative two-marginal Gaussian tail probability
bound before normalization must be ≤0.001. This bound applies to the analytic
effective Gaussian, not an unknown physical PSF tail. Both orientations and
phases and both fields must pass. Criteria are predeclared project defaults, not
an ISO acceptance standard.

Selection minimizes `sum_fields(3*Nfft*log2(Nfft))` over the passing finite grid;
FFT dimensions include the actual replicated halo and linear convolution support.
Measured time is informational only. Peak working-memory is a conservative proxy,
not measured RSS, and is bounded at 1 GiB before allocating a scene. This does not
claim the fastest setting across all algorithms or hardware. Sequential channel
FFTs limit allocation. The selected setting and center phase are included in the
immutable camera-profile JSON/hash and used by the actual unified MTF and Macbeth
renderer. Known synthetic observations use q16/8σ; the original DB native sampled
before kernel remains unchanged. Physical fitted coefficients and parent models
are never modified/refit by the numerical search. Failure preserves a report but
blocks active-model publication; a retained default-renderer holdout in that case
is explicitly unqualified.

The unified page, DB active model card and HTML report show the selected settings.
Reports include all candidate errors/costs, reference refinement checks, eight
kernel/RAW/ISP curve comparisons, fixed-scale RAW/ISP previews, result JSON, CSV,
and per-setting NPZ integer RAW/linear outputs in the manifest and ZIP. The report
PSF plots use the actual chosen kernel spacing/support. Prior reports remain
immutable and are labeled as lacking this new convergence qualification.

Scope remains two independent local probes with edge-replicated boundaries, not
full-sensor crops or field interpolation. The preserved original DB pixel-integrated
PSF cannot be subpixel-refined without new upstream optical data. This numerical
qualification does not validate the Gaussian family, physical phase/spectral PSF,
long-range flare, or real camera measurements.


### Sensor-coordinate convergence crops (v2)

New unified submissions bind the verified IMX462 structural revision to the existing official-document dimension overlay (active 1097x1937, pitch 2.9 um). Optical-axis mounting [548,968], RGGB phase, and crop placement remain declared assumptions. Nine native 128x128 crops use rows 64/484/904 and columns 64/904/1744. The original two virtual probes are retained unchanged.

The original calibrated PSFs remain at their source DB chief-image coordinates, including the outer knot outside this small sensor. Inside the sensor, linear interpolation of Gaussian covariance versus image radius, with rotational symmetry, supplies an explicitly assumed effective model. Spectral DC/response use the same weights. No extrapolation, new field measurements, or new prescription solution is claimed. Per-crop PSFs are stationary; within-crop optical gradients/distortion/flare are outside scope. Geometry, source hashes, knot coordinates, derived nodes and weights are frozen before numerical selection in the immutable camera profile.

One global 3x3 chart function is evaluated by source tile coordinates. Optical halos contain the entire finite kernel support; an even 8-pixel native ISP halo preserves active-array RGGB phase and is removed after ISP. RAW analysis receives the full active shape and actual crop origin. A separate expanded-canvas check renders 16 more pixels per side, then compares the same final crop (36 combinations; RAW exactly equal and ISP max absolute <=1e-12). This verifies located crop/halo/CFA processing, not full-frame spatially varying optics.

The 16-candidate sampling/support search must pass kernel, RAW and ISP curves at both original probes and all nine crops. Reference refinement and tail-energy gates are unchanged. Global minimum estimated FFT work is chosen only after all fields pass; crop parity failure blocks activation. The report includes the sensor footprint, coordinates, interpolation provenance, all field curves, candidate costs and parity JSON, with the same model linked from DB Design Lab.


### Direct DB diffraction at nine sensor crops

New unified submissions replace the interpolated sensor_crops block with calibration_direct_db_sensor_crops_v1. The pinned source lens prescription, operating stop and detector plane are verified against the original DB candidate. A verified stop-center chief ray is inverted at 550 nm to each actual crop anchor; the resulting physical object direction and shared 550 nm image-plane anchor are held fixed at all 21 exact wavelengths from 450 to 650 nm. There is no PSF or wavelength interpolation, Gaussian surrogate, or inherited effective-system blur at those nine locations.

Each field/wavelength independently runs reference-sphere OPD and scalar diffraction with the existing pupil L1 <=0.03, OPD interpolation <=0.02 waves, phase-grid <=0.25 waves and retained-energy 0.95..1.001 gates. Adaptive pupil samples 24->48,48->96,96->192 are bounded. Hash-verified pupil rays and completed fields are resumable in .cache/calibration-direct-psf/<geometry_sha256>. Exact source/operator identities are preserved.

A directly propagated point-PSF reference (q16, radius96 native pixels) is generated independently of the former native pixel-integrated source PSFs. Nested coarser grids select those actual propagated point samples by decimation; source PSFs are never upsampled. D65 and the calibrated replacement QE weight the 21 spectra into RGB lens PSFs. Their unit-energy spatial shape is tested with an explicit normalized 32000-photon/pixel D65 edge; absolute field irradiance/shading is not claimed. One square-pixel area integration follows optical convolution. Separately unidentified charge diffusion remains zero, rather than reusing the old mixed effective Gaussian blur.

The rational-slope 7/100 edge renderer uses exact discrete PSF projections/CDFs instead of allocating a large FFT. This equals full discrete optical edge convolution, followed by native receiver integration, Bayer capture and the unified ISP. Optical source dependency can extend past the active sensor edge; the receiving RAW/ISP halo must stay inside it. Tests compare this execution to an independent asymmetric 2D convolution and expanded-canvas crop readback. No full sensor array is allocated.

The search uses q=1,2,4,8,16 and support radius=8,16,32,48,64 pixels. Optical intensity Nyquist failures are refused before capture. References q8/r64 -> q16/r64 and q16/r64 -> q16/r96 must pass optical RGB MTF, RAW Gr SFR, ISP G SFR and relative finite-window mass gates at all nine positions. Tolerances are 0.005 absolute MTF, 1% in-band MTF50 and 0.001 mass relative to the finite wide reference. Both MTF50 values above the measured band are explicitly marked out_of_band; no crossing is fabricated. Unknown energy beyond the finite reference is not represented as a zero tail bound. Minimum operation-count proxy among this bounded passing search is selected, then 36 expanded-canvas comparisons must pass.

The new direct optical source, candidate/reference RAW/ISP arrays (including native ISP halos), per-wave pupil evidence, PSFs and all comparisons are saved under direct-optics/ and direct-convergence/ and included in the unified report/ZIP. The immutable active profile binds selected per-location kernels by file path and SHA. The old two virtual-probe effective-PSF calibration/holdout remains separately scoped. Unified jobs have a bounded 10800-second worker deadline and 3-GiB total output limit; optical/capture working-memory estimates are bounded at 1 GiB. Any physical, reference, crop or numerical failure blocks activation without substituting an interpolated PSF.

### Full-sensor space-variant direct PSFs (2026-10-18)

New unified workflows apply the nine qualified direct RGB optical PSFs to every
native sensor pixel (1097x1937). Their actual chief-image anchors, including the
half-pixel difference between the central crop center and optical axis, form an
explicit eight-triangle mesh. Nonnegative barycentric weights sum to one at each
native receiving-pixel center. The weight is held over that pixel's area integral.
Outside the anchor rectangle, coordinates clamp to its nearest boundary. This is
an explicit outer-field assumption; nine direct calculations do not validate
optics at every intervening or corner position. Kernel normalization preserves
constant response, not global point-source flux or absolute vignetting. Sensor
mounting, D65 basis, zero unidentified diffusion and missing distortion transport
retain their declared scope. Source and receiving-pixel apertures are not mixed.

`calibration_spatial_psf.capture_applied(profile, result_root, scene, output_dir)`
executes a hash-bound, successfully validated active profile. Renderer identity,
NumPy/SciPy versions, triangles, selected q8/r64 kernels and file hashes are part
of that immutable profile. A changed implementation or unvalidated application
is refused. The continuous `EdgeAtlas` scene and the general `NativeRaster`
scene sampler use the same optical renderer. NativeRaster represents a native
RGB radiance image as a piecewise-constant source reconstruction, distinct from
the one receiving-pixel aperture. RGB shapes are conditional on the frozen D65/QE
spectral basis, not a new arbitrary spectral scene solution.

The renderer uses native 128x128 output tiles, optical input halo 65 pixels,
sequential RGB channels and circular-FFT overlap-save. The entire finite q8/r64
optical support lies inside each valid output region, so no neighboring-tile
wraparound reaches an output pixel. A 96-MiB LRU bounds kernel spectra; a 640-MiB
preflight bounds estimated optical arrays. The default estimate is about 500 MiB,
versus about 3112 MiB for just one full-sensor q8 RGB float64 array. This is an
array working-set estimate, not measured total-process RSS. No full supersampled
scene or per-pixel PSF map is allocated. Native RAW and camera-linear RGB are
written to disk-backed NPY arrays. Calibrated readout and CCM remain fixed; the
same receiving-pixel area is integrated once after optical convolution.

Reference ISP frames are bounded and even, so the odd native sensor extent is
processed using even global origins, an 8-pixel ISP halo and phase-preserving
reflection at active boundaries, then trimmed to its original 1097x1937 shape.
No zero-filling, CFA phase restart or halo loss occurs at the final partial tile.

Four full-sensor nine-panel continuous slanted-edge charts (0/90 degrees,
0/.25-pixel phase) are saved. The first chart is independently repeated using
96-pixel tiles; all 2,124,889 active pixels, including seams and odd borders, must
match integer RAW exactly and ISP to 1e-12. All 36 extracted spatial crops must
match separately rendered, differently partitioned spatial windows to the same
limits. An additional fixed-anchor FFT comparison reproduces each previous CDF
Crop, isolating convolution, area, CFA and ISP implementation from spatial-model
changes (RAW <=1 DN, ISP <=1e-4, MTF <=1e-5, in-band MTF50 relative <=1e-4).

The space-variant crops are also compared directly with the previous stationary
crops using RAW/ISP MTF curves, MTF50 status and residuals. Those differences are
reported as model changes, not forced to zero or silently used to retune the
physical parameters. Missing/out-of-band MTF50 stays explicit. Partition,
independent-window or fixed-renderer failure blocks unified activation. The
report adds full-sensor RAW/ISP previews and native downloads, the triangle map,
36-row comparison CSV/JSON, local MTF curves and separate implementation-parity
and model-change tables. DB Design Lab shows the active full-sensor policy and
links to the same immutable unified workflow/report. Previous reports and source
runs are preserved.


### 센서 끝 직접 앵커와 공간 밀도 검증

통합 보정 워크플로는 내부 9개 Crop PSF의 수치 검증을 보존하면서,
전체 활성 센서를 덮는 3×3(9점), 5×5(25점), 셀 중심을 추가한 41점
직접 PSF 후보를 비교한다. 네 모서리는 `[0,0]`, `[0,W-1]`,
`[H-1,0]`, `[H-1,W-1]` native 픽셀 중심이며 수신 영역 밖 외삽은 거부한다.

41점 후보의 셀 중심 16개와 별도로 새 16개 직접 계산 위치를 고정한다.
이 최종 검증점은 어느 후보에도 앵커로 사용하지 않는다. 9·25점 후보는
기존 셀 중심 16점도 추가로 검사해 알려진 실패점을 누락하지 않는다. 각 위치의 21개
파장 광선·OPD·회절 결과, q8/r64 대 q16/r96 수치 검증과 해시를 보존한다.
PSF L1≤0.03, 광학/RAW/ISP MTF 곡선 차이≤0.005, 대역 내 MTF50
상대 차이≤1%를 모두 만족하는 유한 후보 중 실행 앵커 수가 가장 적은 것을
선택한다. 더 거친 후보는 더 촘촘한 전체 센서 차트 비교도 통과해야 한다.
아무 후보도 통과하지 않으면 보고서는 남기고 DB 활성화를 차단한다.

전체 native RAW·linear ISP를 동일 25패널 차트, 두 방향·두 위상으로
재생성한다. 25↔41점의 100개 위치·차트 MTF 비교, 선택 후보의 독립 Crop
100개 일치와 타일 128↔96 전체 픽셀 일치를 보고한다. 마지막 행·열은
전체 프레임 검사에 포함하며, 분석 Crop은 CFA 위상 보존을 위해 짝수 원점이다.
`boundary_spatial_psf`를 저장한 활성 프로파일은 기존
`calibration_spatial_psf.capture_applied` 진입점에서 새 경계 렌더러로 연결된다.

커널 LRU는 4개, FFT LRU는 96 MiB로 제한한다. 광학 halo 65 pixel,
ISP halo 8 pixel을 사용하며 native RAW·ISP는 디스크 배열이다. 광학 배열
추정과 전체 프로세스 RSS는 구분한다. 추가 직접 커널·광선 증거와 전체 센서
프레임·ZIP을 함께 보존하기 위한 워크플로 저장 한도는 5 GiB다. 고해상도
콜드 캐시의 전체 광학 수치 검증에는 유한 6시간 worker deadline을 적용한다.
q16/r96 중간 PSF는 새 앵커마다 처리 후 해제하며 광선·비교 곡선·참조 RAW와
실행 q8 커널을 보존한다. 기존 내부 9개 원본 PSF·과거 기록은 유지한다.

수신 좌표에서 정규화한 공간 가변 PSF이므로 상수 응답을 보존한다. 절대
비네팅·점광원 총 에너지·왜곡 운반 또는 실물 카메라 일치 검증은 미확립이다.
공간 수렴은 분리된 유한 직접 검증점과 차트 대역에 한정되며, 연속 공간의
정확해나 물리 검증으로 해석하지 않는다.


전체 센서 프레임 캐시는 전체 서버 코드 지문과 별도로 픽셀 연산의 수치 계약을
고정한다. 수신 좌표 가중치·커널·전자/ISP 파라미터·차트·타일·수치 라이브러리와
실제 연산 함수/의존 코드 해시가 같을 때만 SHA를 검증하여 재사용한다. API나
작업 등록 코드의 변경으로 이전 작업이 무효화돼도, 완료된 수치 결과의 원본
실행 상태와 이력을 보존한 채 새 작업에서 재검증할 수 있다. 캐시는 새 작업의
소스 가드·독립점/MTF/Crop·전체 타일 검증·holdout·CAS를 대체하지 않는다.
활성 프로파일의 `capture_applied`는 캐시를 사용하지 않고 실제 연산을 수행한다.
보고서는 재사용 프레임 수·원본 실행·원본 계산 시간·복원 시간을 구분한다.
