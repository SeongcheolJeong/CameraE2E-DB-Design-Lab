# Camera Platform dev24: 입력 장면과 기존 CameraE2E ISP 연결

이 문서는 [dev20의 광학·MTF·HDR·인지 근거 경계](camera-platform-joint-workflows-dev20.md)를 유지하면서, 예제 밖 장면을 Lens DB × Sensor DB RAW 계산에 넣고 저장 RAW를 기존 CameraE2E ISP로 재처리하는 개발 계약을 설명한다. **두 작업은 현재 서로 다른 장면/RAW를 사용한다.** 사용자 등록 분광 장면은 Lens×Sensor 정수 RAW와 플랫폼 ISP v2·데이터셋 export까지 연결된다. 기존 `pyisetcam.ip_compute()` 색 경로는 명시적인 D65 균일장 또는 slanted-edge에서 촬영해 저장한 RAW에만 연결된다. 임의 조명의 AWB·CCM을 암묵적으로 추정하지 않는다.

## 사용자 분광 장면 → 기존 PSF convolution → 센서 DB RAW

`GET /api/v1/lens-sensor-radiance-scenes/template` 또는 `PlatformClient.lens_sensor_radiance_scene_template()`에서 가져온 metadata에 실제 출처·license·생성 방법을 기록하고, C-order float32/64 `.npy`를 `import_lens_sensor_radiance_scene(metadata, path)`로 등록한다. 입력은 렌즈 앞 물체 방향의 **분광 radiance** `W/m²/sr/nm`이며, 센서면 irradiance·RGB 사진·후처리 영상이 아니다. 높이·너비는 짝수 32–256, 분광 표본은 3–16개이고 파장축의 범위는 450–650 nm를 덮어야 한다. 원본 NPY와 metadata를 바이트·SHA-256으로 보존한다. 450/550/650 nm 에너지 밀도를 보간한 뒤 광자로 한 번 변환하고, 밝기 목표로 재정규화하지 않는다.

`client.lens_sensor_raw_template(scope="central_bounded_radiance_scene")`의 v6 요청에 선택한 Lens DB revision, 저장 Sensor DB run ID, `radiance_scene_id`, 노출·seed를 넣고 렌즈 광량 근사 수락을 명시한다. 제출 전 저장 렌즈 PSF의 conjugate와 **대각 화각**을 검사한다. 범위를 넘는 장면은 Worker에 넣지 않고 crop 또는 다른 렌즈 선택을 요구한다. 실행은 기존 `pyisetcam`의 저장 기하 raytrace PSF convolution과 센서 DB `db_estimate_v1` 분광 응답·readout을 사용하며, 정수 RAW와 출처를 저장한다. 기존 `spectral_images` 자산은 센서면 irradiance 계약이므로 이 입력으로 조용히 재해석하지 않는다.

이 경로는 사용자가 만든 임의 장면의 물리적 참값을 검증하지 않는다. 입력 스펙트럼을 세 파장으로 축약하므로 그 사이의 좁은 분광 특징도 보존하지 못한다. 현재 저장 Lens PSF에는 회절이 없고, 렌즈 분광 투과율·상대조도는 검증되지 않아 unity 가정이다. CRA와 픽셀 입사각 응답도 결합되지 않았다. 사용자 장면의 `measured` 출처 표기는 선언일 뿐 독립 측정 검증이 아니다. 그러므로 수치 RAW는 **지정 모델 안의 민감도 진단**이며 제품별 광량·화질 순위는 허용하지 않는다.

## 별도 저장 D65 RAW → 기존 CameraE2E 색 ISP

`GET /api/v1/lens-sensor-legacy-isp/template?source_lens_sensor_job_id=<id>` 또는 SDK의 `lens_sensor_legacy_isp_template()`은 완료된 D65 Lens×Sensor RAW의 원본·CFA·readout·센서 revision을 확인한다. 색 한계 확인 필드를 지정해 제출하면 기존 `pyisetcam.ip_compute()`가 정확히 같은 저장 int32 RGGB RAW에 `bilinear`, `laplacian`, `adaptive_laplacian` 중 하나를 적용한다. 저장 EQE에서 얻은 D65 Macbeth sensor→XYZ 적합과 CameraE2E Display 모델을 쓰고, 원본 RAW, 정규화 입력, sensor RGB, XYZ, display RGB, sRGB를 별도 NPZ로 보존한다. 획득을 다시 실행하지 않으며 세 demosaic 방법 비교에서 같은 RAW와 같은 모델 기반 색 행렬을 유지한다.

설치된 child Worker에서 `ip_create()`가 내부 Display를 만들 때 기본 자산 저장소를 조회하는 문제를 수정했다. ISP 계산 전체에 해시로 고정한 **로컬 ISETCam 자산**을 묶어 외부 다운로드 없이 실행한다. 자산이나 sensor snapshot이 바뀌면 실행을 거부한다. 이 색 행렬은 실측 CCM·AWB가 아니며 ΔE나 색 정확도를 검증하지 않는다. 사용자 등록 임의 분광 장면의 RAW는 이 D65 전용 ISP 작업의 입력으로 승인하지 않는다.

## 설치본 검증과 남은 판단 경계

[dev24 wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev24-py3-none-any.whl)의 SHA-256은 `b9a4aa07d542b60ef6a9a503e1757afceb543e4f3b149daa6e5ec01eb2adc86d`다. 별도 경로에 설치한 wheel의 1,036개 파일이 원본과 일치했고, 새 Registry와 child Worker에서 [설치본 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev24-radiance-legacy-isp-installed-proof.json)를 생성했다. 등록한 32×32×3 장면은 8×8 정수 RAW로 계산돼 min/max 88/230 DN을 냈다. 다른 16×16 D65 RAW는 기존 ISP의 Adaptive Laplacian으로 처리했고, 전후 원본 RAW content SHA-256이 일치했다. 가로 화각 20°인 정방 장면의 대각 화각이 저장 렌즈 시야를 넘어서는 경우는 **제출 전에 거부**돼 작업이 큐에 들어가지 않았다. 이전 dev21은 템플릿 scope 누락, dev22는 중첩 Display의 외부 자산 조회 때문에 설치본 증명에 실패했다. 해당 wheel은 실패 기록으로 남겨 두었다.

SDK의 등록→제출→대기→결과 조회 순서는 [설치본 검증 스크립트](../tools/verify_camera_joint_dev24_installed.py)의 `_radiance()`와 `_legacy_isp()`에 각각 재현돼 있다. 두 함수가 서로 다른 source RAW job ID를 사용하는 점이 현재 계약의 중요한 경계다.

이 증명은 계산·저장·SDK·Worker 통합의 증거다. 기존 8770 서버 교체, 광학 실측, 색 정확도, HDR/LFM 제품 모드, 인지 정답의 독립성, 범용 고급 ISP·다목적 최적화 또는 상용 제품 준비의 증거가 아니다. 특히 [dev20의 공통 물체각 격자 MTF](camera-platform-joint-workflows-dev20.md)는 유한 픽셀 적분의 유효 각도 면적이 아직 일치하지 않아 제품 순위에 사용할 수 없다. HDR fusion의 현재 결과는 `inconclusive`이고 작은 합성 인지 fixture의 mAP는 연결 진단이다.

다음 물리 검증의 우선 입력은 같은 lens revision·개구·초점·field·파장에 맞춘 **회절 포함 PSF/OPD 및 투과율·상대조도**, 그리고 같은 센서 구조·파장·입사각의 픽셀 응답이다. 현재 저장 Lens v2 자산 네 건은 기하 PSF만 갖고 OPD/복소 동공이 없으며, radiometry와 센서 sampling 검증도 미완료다. 소스 처방에는 순차 행이 28개 또는 36개인데 현재 `lens_factory`는 최대 24면을 허용한다. 마지막 `image` 표기 행도 일부 구성에서는 비영(非零) gap·곡률이 있어 임의로 버릴 수 없다. 재질 분산과 개구 규약도 검증되지 않았다. 기존 `prescription_psf`의 RayOptics→Prysm 회절 경로를 이 DB revision에 그대로 연결하거나 기하 PSF에 Airy blur를 더해 제품용 회절 PSF라고 부를 수 없다. 다음 구현은 revision별 처방 변환·검사와 조건에 맞춘 OPD/광량 재계산을 **연구용 proxy**로 연결하는 것이다. CRA×픽셀 각도 결합과 제품 순위는 짝이 맞는 native angular response가 마련될 때까지 막는다.

그 계약과 픽셀 적분 면적이 검증되기 전에는 MTF·RAW의 상대 차이를 제품 판단 점수로 승격하지 않는다. HDR은 vendor mode·line timing·실제 die 위치와 유효 픽셀 coverage가 필요하고, 인지는 독립 주석·대표 장면과 실제 detector 추론이 필요하다. 그 후 범용 ISP와 최적화의 목적·제약을 연결해야 한다.
