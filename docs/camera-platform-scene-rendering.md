# 3D Scene · 분광 렌더링

**현재 기본 설치 작업실은 v0.13.0이다.** [저장된 분광 복사휘도 재사용](camera-platform-scene-projection.md)을
연결하여 같은 광선 격자에서 새 투과율·조리개와 대상 카메라의 RAW/RGB를 계산한다.
초점거리·격자·파장·장면 조건이 달라지면 재렌더링이 필요하다.
별도 설치 QA의 [기존 장면 작성 36항목](../reports/camera_platform/projection-ui/authoring-v013/verification.json)과
[새 재사용 26항목](../reports/camera_platform/projection-ui/installed-v013/verification.json), 기본 작업실의
[7개 흐름·168항목](camera-platform-installed-runtime.md#기본-작업실의-화면-검증)을 확인했다.
전체 인도 근거와 남은 범위는 [v0.13 인도 기록](../reports/camera_platform/delivery.json)을 따른다.

**다음은 v0.12.0 인도 당시의 기록이다.** 원문 §7.1의 Geometry/Material/Light/Scene 분리와
기존 Mitsuba → PSF → RAW/RGB에 [장면별 카메라 준비](camera-platform-scene-camera.md)를 연결했다.
[기본 설치 전환](../reports/camera_platform/primary-preparation-upgrade.json)은 기존 208개 기록·68개 작업·412개 감사 이벤트를 보존했고,
[새 구성의 실제 실행](../reports/camera_platform/primary-preparation-verification.json) 7항목을 통과했다.
전체 회귀 1,158개·별도 전환 36개 중 6개는 중복이며, 설치본 실행/재시작은 14항목을 확인했다.
별도 설치 QA의 [장면 작성 36항목](../reports/camera_platform/preparation-ui/authoring-v012/verification.json)과
[카메라 준비 20항목](../reports/camera_platform/preparation-ui/installed-v012/verification.json)은 해당 QA의 화면 근거다.
기본 작업실의 [읽기 전용 화면 검사](camera-platform-installed-runtime.md#검증과-한계)도
6개 흐름·141항목을 통과했다(POST 0회·브라우저 오류 0건). [최종 보존 검사](../reports/camera_platform/primary-preparation-integrity.json)와
[기존 시간 RAW/RGB 재실행](../reports/camera_platform/primary-preparation-legacy-rerun.json)을 함께 확인했다.

v0.11.0의 3D 작성/렌더링 인도와 후속 개발 시험 1,143개·브라우저 17항목은 역사적 기록이다.
일반 동적 장면·재질, 시야/거리별 광학 응답, 총 불확실도와 실물 검증은 남아 있다.
실제 장비 측정은 0건이며 원문 전체 플랫폼 Goal은 미완료다.

## 작성 작업실

`#scenes` 화면은 형상·재질·광원을 독립적으로 등록하고 기존 자산의 새 revision을 만든다.
장면은 형상/재질 선택, 환경광, m 단위 배치와 전체 transform JSON을 제공한다. 재질 반사율과
광원 분광 값, 렌즈 투과율, 촬영 위치/방향·크기·표본 수는 입력칸에서 편집한다. 고급 조건과
출처는 JSON에서도 보존한다. 기존 자료를 수정할 때 부모 ID를 남기고 기본 출처를 추정으로 설정한다.

렌더링 양식은 실제 고정 Lens의 초점거리/F-number를 읽으며 미제공 투과율을 `null`로 둔다.
잘못된 광학/조명 조합을 실행 전에 거부한다. 같은 화면의 **이 장면의 새 카메라 준비**에서
해당 조명에 적용 가능한 모델과 조건을 검사하고 새 구성을 저장한다. 실제 Runtime의 파장·각도·
Gain·RAW 계약을 최소 8×8 영 광량 입력으로 검사하며 입력·모델을 임의로 보정하지 않는다.
이 준비 검사는 실제 복사휘도, 렌더링 근사 오차 또는 물리 예측 정확도의 검증이 아니다.

파장별 미리보기는 **한 렌더링의 모든 파장에 같은 선형 광량 범위**를 사용한다. 서로 다른
렌더링의 표시 최대값은 같다고 보장하지 않으므로 수치 비교에는 원본 배열과 표시 범위를 쓴다.
깊이는 실제 first-hit 거리이며 표면 미검출은 검정이다. PNG는 표시용이고 부동소수점 NPY가
원본 근거다. 완료 결과에서 Camera 입력·공통 시험·실제 RAW/RGB를 생성하고 실험 화면으로 연결한다.

조회 오류는 작업 실패와 구분한다. 같은 작업 ID로 명시적으로 다시 조회하며 자동 요청 폭주나
새 계산 제출로 대체하지 않는다. 시험 재사용은 지표·단위·관측/효과·조건·허용 오차를 포함한
전체 생성 계약의 일치를 요구한다. 늦은 응답은 사용자가 이동한 화면을 강제로 되돌리지 않는다.

아래는 v0.11 인도 전의 작성 기능 검증 기록이다. 현재 설치 QA 근거는 문서 맨 위를 따른다.

- [v0.11 인도 전 작성 기능 회귀시험](../reports/camera_platform/scene-authoring-full-tests.xml): **1,115개 통과**, 실패/오류/skip 0.
- [실제 브라우저 작성·Worker 계산](../reports/camera_platform/scene-ui-final/verification.json): **36항목**.
  원본 유지, 재질 수정, 분광 투과율 입력, 실제 3D→RAW/RGB, 깊이 동일성, 조회 실패/재시도,
  재진입 및 Desktop/Mobile 표시를 확인했다. 저장된 자료는 모두 합성 사례다.
- [시험 재사용·응답 경합](../reports/camera_platform/scene-ui-final/test-reuse.json): **4항목**.
- [런처 시험](../reports/camera_platform/scene-launcher-tests.xml): **31개**.
  [실제 재실행](../reports/camera_platform/scene-launcher-live.json)은 별도 shell 환경 없이
  저장된 Mitsuba venv 경로와 실제 backend가 일치함을 확인한다.

## 실제로 연결한 흐름

형상·분광 재질·광원을 각각 불변 `scene_assets`에 등록하고 Scene 배치에서 정확한 버전을
참조한다. `scene_renders` 작업은 기존 SQLite Queue와 별도 Worker를 사용한다. Worker가
격리된 Mitsuba Python을 실행해 파장별 복사휘도와 깊이·가림 정보를 계산한다.
완료 결과의 명시적 광학 변환을 `spectral_images`에 게시하면 기존 공간 Test/Camera/ISP/
근거 ZIP 흐름에서 소비한다. 사람과 SDK는 동일 API를 쓴다.

- Geometry: m 단위 sphere/rectangle/cube와 삼각형 mesh, 오른손 좌표계, 배치별 affine transform.
  Sphere는 균일 축척만 허용하고, 퇴화 삼각형·잘못된 인덱스·특이/반전 변환을 거부한다.
- Material: 분광 Lambertian 반사율 또는 GGX rough conductor의 eta/k·roughness·표면 상태.
  형상과 재질이 별개이므로 같은 형상에 다른 재질을 적용한다.
- Light: 정적 환경/면 광원의 복사휘도 `W/m²/sr/nm`, 점 광원의 복사강도 `W/sr/nm`.
  Scene은 광원의 정확한 버전과 배치를 고정한 `illumination_id`를 제공한다.
  Assembly의 `conditions.light_id`가 이 값과 다르면 실행 전에 거부한다.
- Scene: 형상/재질/면광원 연결과 transform, 환경광, 점광원 배치를 고정하고 원본 JSON·
  각 의존 자산의 snapshot을 보존한다. 메타데이터의 출처는 선언된 출처이며 진위 인증은 아니다.

## 물리적 출력 경계

Mitsuba `scalar_spectral`의 `SamplingIntegrator.sample`에 **같은 파장으로 채운 4개 lane**을
전달해 각 지정 파장의 경로 복사휘도 `Lλ`를 계산한다. RGB/XYZ film이나 특정 Sensor QE로
적분하지 않으므로 RGB에서 스펙트럼을 복원하지 않는다. 출력 파장은 Sensor와 정확히 일치하고,
재질·광원 표는 이 파장 범위를 포함해야 한다. 표 사이의 선형 보간과 경로 최대 깊이의 오차는
전체 불확실도에 포함해 평가해야 하며 현재 정량 경계는 unknown이다.

렌더러의 film과 SRF 출력은 의미가 서로 다르다.
[Mitsuba film 문서](https://mitsuba.readthedocs.io/en/stable/src/generated/plugins_films.html)와
[SamplingIntegrator API](https://mitsuba.readthedocs.io/en/stable/src/api_reference.html#mitsuba.SamplingIntegrator.sample)를
확인하고, 이 adapter는 film을 우회해 지정 파장의 복사휘도를 저장한다.

기존 Camera 입력으로의 변환은 별도 선언이다.

```text
Eλ = π Lλ Tλ / (4 N²)
Lλ: W/m²/sr/nm
Eλ: W/m²/nm, sensor_ideal_image_before_psf_ri
Tλ: 출처와 방법을 선언한 on-axis transmittance
N: 고정된 Lens property 및 Assembly 조건과 같은 F-number
```

이는 on-axis 근축 근사다. 유한 물체 거리의 배율/출사동공·초점 심도, 실제 Lens의 비네팅/
왜곡/수차를 추출한 모델이 아니다. 투과율은 임의로 추정해 채우지 않는다. 요청에 명시된 값과
출처·방법을 보존한다. Native sensor의 pitch·전체 크기·crop·선언 focal length로 픽셀 중심의
pinhole 광선을 만들며, 공간 적분/anti-aliasing/왜곡은 포함하지 않는다.

이 출력에는 on-axis throughput만 포함된다. 이후 기존 Runtime이 PSF·relative illumination·
각도별 QE·전자/Readout·ISP를 적용한다. Cos⁴, PSF, QE, pixel aperture를 렌더러에서 중복 적용하지
않는다. 게시된 입력은 projection Lens ID를 보존하며 공간·시간·폐루프 Runtime이 다른 Lens를
선택하면 재투영을 요구한다. 모든 경로의 총 예측 불확실도와 실물 정확도는 미확립이다.

## API와 실행 환경

| API | 역할 |
| --- | --- |
| `GET /api/v1/scene_authoring/templates` | 자산 초안과 JSON Schema |
| `GET /api/v1/scene_authoring/render-template` | 장면/Camera 조건, 미제공 광학 값, 연결 문제 |
| `POST /api/v1/scene_assets` | 형상·재질·광원·Scene revision 등록 |
| `GET /api/v1/scene_assets/{id}` | 원본·의존 자산·illumination ID 조회 |
| `GET /api/v1/scene_renders/catalog` | 실제 backend probe와 지원 범위 |
| `POST /api/v1/scene_renders` | 고정 입력의 durable 렌더링 작업 제출 |
| `GET /api/v1/scene_renders/{id}` | 상태·원본·출력·backend identity 조회 |
| `POST /api/v1/scene_renders/{id}/cancel` | 기존 Worker 취소 경로 사용 |
| `POST /api/v1/scene_render_publications` | 완료 결과를 Camera의 분광 입력으로 게시 |
| `GET /api/v1/scene_renders/{id}/bundle` | 렌더링 원본/출력 근거 ZIP |

SDK의 `create_scene_asset`, `render_scene`, `wait_scene_render`, `publish_scene_render`를
사용할 수 있다. 실제 예제는 [HTTP 검증 도구](../tools/verify_camera_scene_http.py)를 참고한다.
관측 timeout은 작업 종료가 아니므로 동일 ID를 다시 조회한다.

Native 크롭을 수신 ROI로 사용하면서 렌더 입력의 PSF halo까지 확보하려면 렌더 요청에
`render_psf_halo: true`를 지정한다. 이때 요청의 `shape`와 `crop_origin`은 수신 ROI이고,
제출 단계가 고정된 Lens PSF의 커널 반경을 읽어 실제 소스 렌더 범위를 확장한다.
물리 센서 경계에서 잘리고, 저장 가능한 짝수 Bayer 크기를 위해 필요한 경우 실제 센서 픽셀
한 칸을 더 포함한다. 확장된 범위가 렌더 크기·일차 경로 예산이나 Field PSF 영역을 벗어나면
실행 전에 거부한다. 완료 결과의 `capture_crop`은 게시된 분광 이미지의 렌더 snapshot에
고정되며, `spatial_scene_v1` 실행에서 파라미터를 생략하면 이 수신 ROI를 사용한다.
게시된 분광 이미지의 native sampling은 확장된 **소스** 범위를 기록하며,
렌더 요청·결과에는 의도한 수신 ROI와
사용된 PSF 출처가 남는다. 렌더러 자체는 PSF를 적용하지 않는다.

생성 전하와 인접 픽셀 수집까지 있는 Sensor는 `render_charge_collection_halo: true`를
선택한다. 이때 수신 ROI로 들어올 수 있는 source site를 Sensor의 고정 수집 offset으로
역산하고, 그 generation 범위를 Lens PSF 반경만큼 더 확장한다. 요청·결과에는 Sensor
ID·모델 해시·수집 artifact 해시·offset·generation 범위와 수신 ROI를 고정한다.
출판과 공간 RAW 실행은 이 결속을 재검사한다. 이 모드는 파장·장면·PSF·수집 LUT의
계산 연결을 검증하며 물리 센서의 실제 누화 정확도를 보증하지 않는다.

Mitsuba 3.9.1 / Dr.Jit 1.5.0 / NumPy 2.4.2를 별도 `.venv-scene-render`에 설치해 확인했다.
서버의 신뢰된 환경 변수 `CAMERA_PLATFORM_MITSUBA_PYTHON`으로 인터프리터를 지정한다.
또는 런처 `--mitsuba-python /absolute/venv/bin/python`으로 고정 adapter probe와 현재 서버의
일치를 검사한 뒤 `runtime-config.json`에 저장한다. 다음 Finder/런처 실행은 이 설정을 복원한다.
기존 Meep 설정을 보존하며, 가상 환경의 symlink 경로를 실제 binary 경로로 치환하지 않는다.
Job 본문에서 임의 script/plugin/path를 실행하지 않는다. 실행 파일·adapter 해시와 backend
버전을 고정한다. 이 Mac의 LLVM 초기화 경고는 로그에 보존되며, 실제 계산은 사용 가능한
`scalar_spectral` CPU variant로 수행된다. GPU/LLVM backend 검증 근거는 없다.

현재 360–830 nm 내 2–16개의 명시적 파장, 8–256의 짝수 native crop, 최대 262,144개의
일차 경로, 최대 100,000개의 배치된 mesh triangle로 제한한다. 이것은 처리 용량의 합격
벤치마크가 아니다. 렌더링 subprocess 제한 110초와 기존 Worker timeout/cancellation을 사용한다.

## 검증 근거와 남은 작업

- [전체 회귀시험](../reports/camera_platform/scene-full-tests.xml): **1,101개 통과**, 실패/오류/skip 0.
- [3D 직접 시험](../tests/platform/test_scene_render.py): 고정 파장 면광원 방사량, 점광원
  Lambertian/거리 제곱 기준, 검은 mesh의 가림, 재질 교체, GGX conductor, 잘못된 계약 거부,
  실제 Prysm→RAW/RGB 및 원본 ZIP 보존.
- [HTTP/별도 Worker 결과](../reports/camera_platform/scene-http-verification.json): 재사용 형상과
  고정 조명 아래 red/blue 재질 교체가 분광 복사휘도·실제 RAW·RGB 해시를 바꾸고, 깊이/가림과
  원본 자산·렌더링 결과까지 ZIP에 포함됨을 확인한다. 모든 자료는 합성 데이터다.
- 취소된 렌더링도 원본 입력 ZIP을 보존하며 결과 게시를 거부한다. 이 보완은 HTTP 검증 이후
  추가하고 최종 전체 회귀시험으로 확인했다. 보고서별 환경 해시를 해당 증거의 범위로 읽는다.
- `radiance-mc-standard-error.npy`는 유한 표본의 통계적 표준오차 추정이다. 0으로 나와도
  모델 오차·분광/공간 표본화·경로 truncation·광학 근사 오차가 0이라는 뜻이 아니다.

다음은 작성 화면을 포함한 설치 패키지와 별도 설치 Worker/UI 검증·기본 작업실 인도, 독립 수치 수렴 검사와
불확실도 전파, radiance 원본의 재투영/재사용, 일반 재질/텍스처/매질·BSDF 교환·움직임·
시야별 광학이다. OpenMATERIAL 입출력은 아직 미구현이며, 이번 개발로 원문 §7.1 전체나
상용제품 목표를 완료했다고 판정하지 않는다.

![합성 재질 교체의 실제 RGB, RAW, 깊이 비교](../reports/camera_platform/scene-material-comparison.png)

그림의 RGB 표시에만 동일한 4배 배율을 적용했다. 저장된 RGB와 RAW를 변경하지 않았으며,
공통 RAW 표시 범위와 깊이 축을 사용한다. 이 합성 사례는 제품의 실물 색 정확도 근거가 아니다.
