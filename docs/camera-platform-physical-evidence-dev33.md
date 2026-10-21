# Camera Platform dev33: 광학–픽셀 연결과 평가 입력 근거

이 단계는 기존 CameraE2E의 PSF 적용을 다시 만드는 작업이 아니다. 저장된 기하
raytrace PSF의 영상 계산은 이미 있다. [dev32](camera-platform-physical-evidence-dev32.md)는
실제 Lens DB p0014 처방에서 광량·입사 방향과 회절 PSF를 각각 실행하고, 같은
처방의 연구용 draft Lens 모델로 등록했다. 이번 단계는 그 다음에 필요한 원본
Sensor 각도 응답의 등록, MTF 입력 지지, HDR/LFM 기록 정합을 강화한다.

## 원본 Sensor 응답을 Lens와 결합하는 입구

[`native_response_publication.py`](../src/camera_platform/native_response_publication.py)는
원본 파일과 독립 R/Gr/Gb/B 수집 EQE의 파장·극각·방위 격자를 가진
`camera_native_response_bundle_v1`을 SHA-256으로 고정한다. qualification은
제출자가 보고한 정보이며 그 자체가 실물 검증은 아니다. 응답은 실행된 Factory
Lens의 PSF·입사 방향·파장·sampling pitch와 맞아야 한다. 별도 Sensor readout
모델의 자산이 같은 `component_id`와 `revision_id`를 선언하고, pitch·RGGB
형식도 일치해야 한다. 등록된 모델은 검토에 사용한 **정확한 Lens ID와 해시**에
묶여, 다른 Lens와의 Assembly 검사는 실행 전에 실패한다.

API `POST /api/v1/native-response-bundles/import`와 SDK
`publish_native_response_bundle()`은 서버의 임의 파일 경로를 받지 않고 ZIP
bytes를 전송한다. 압축 64 MiB·해제 128 MiB 제한이 있다. 원본은 다운로드
가능한 내용 주소 artifact로 보존되며, 동일 bytes를 공유하는 파일의 경로·역할
및 자산별 소유권도 유지한다. 등록 결과는 `draft`,
`physical_validation=not_established`, `product_ranking_allowed=false`다.
자세한 계약은 [등록 안내](camera-platform-native-response-publication.md)에 있다.

실제 RayOptics/Prysm Factory fixture로 Lens 모델을 만들고, 합성 4-site Sensor
원본 번들을 ZIP API/SDK로 등록해 8×8 spatial RAW를 실행했다. 이 검사는
원본→모델→RAW 코드 연결과 실패 차단의 검증이다. **현재 자산에는 p0014에
해당하는 검증된 각도별 4-site 수집 EQE가 없다.** 따라서 p0014 Lens와 실제
Sensor의 물리 RAW나 제품 비교가 이 테스트로 완성됐다고 주장하지 않는다.

[dev33 wheel](../reports/camera_platform/integrated-workflows-20260928/dev33-wheel/pyisetcam-0.16.2.dev33-py3-none-any.whl)을
격리 설치해 같은 코드의 핵심 API·RAW·HDR 패킷·MTF 진단 4개를 실행했다.
[설치 파일 대조](../reports/camera_platform/integrated-workflows-20260928/dev33-installed-package.json)는
설치기가 다시 쓴 `RECORD`를 제외한 1049개 wheel 멤버의 bytes가 모두
동일함을 확인한다. [설치본 실행 근거](../reports/camera_platform/integrated-workflows-20260928/dev33-installed-functional.json)는
사용한 ISETCam 원본 스냅샷 revision과 tarball SHA를 기록한다. 설치본에서
분광 chart 생성은 이 약 499 MB 원본 스냅샷에 의존한다. 네트워크가 차단된
운영 환경에서는 `PYISETCAM_UPSTREAM_ROOT`로 미리 확보한 스냅샷을 지정해야
한다. 관련 소스 회귀는 [76개 통과](../reports/camera_platform/integrated-workflows-20260928/dev33-source-qa.json)했고
변경 Python 파일의 Ruff 검사도 통과했다. 이는 설치·운영 및 구현 검증이며
장치 성능 검증은 아니다.

## MTF 점수의 입력 범위

카메라마다 native 픽셀 면적이 다르다는 사실 자체가 MTF 비교 금지 사유는
아니다. 같은 물체각의 점수화 ROI를 확보한 뒤, 각 시스템의 광학 PSF·픽셀
적분·ISP가 그 ROI를 만들 때 필요한 **입력 장면의 범위**와 수렴을 확인해야 한다.
새 품질 Job은 실행된 RAW의 장면 크기·OI sampling·전역 최대 비영 PSF 범위를
소스와 함께 기록한다. 이 전역 최대치는 충분조건의 검사일 뿐이다. 장면 안에
들어오지 않는다고 해서 점수화 영역의 국소 커널이 실제로 잘렸다고 단정하지
않고 `scored_local_input_support_verified=null`로 남긴다. 저장된 RAW에는 mosaic
subpixel/OCL 적분 범위도 없다.

별도 [해시 고정 원본 PSF 에너지 감사](../reports/camera_platform/mtf-support-20260928/saved-dev29-psf-scene-energy.json)에서
기존 2×2 작업의 장면은 256×256 OI 샘플인 반면 전역 최대 비영 커널은
849×849 및 709×709이었다. 실행 간격으로 복원한 **중심 시야 450 nm** PSF의
질량은 중심 256×256 창에 각각 26.14%, 26.12% 포함됐다. 이는 무한 균일
입력에 대한 수치 반례이며, 실제 경사 에지 결과나 제품 MTF 오차율이 아니다.
자세한 조건과 다음 입력 규모는 [MTF 근거 문서](camera-platform-mtf-support-qualification.md)에
기록했다. 물리 MTF와 제품 순위는 계속 보류한다.

## HDR/LFM 기준자료

새 비교 제출은 mode·waveform·reference 로그를 구조화된 JSON으로 받고,
2×4 RAW 프레임 순서·개별 SHA, CFA crop 위상, 노출·행 시간·PWM 위상,
기준 rate 위상과 교정 선언을 동결된 HDR Job 및 업로드 배열과 대조한다.
Worker와 재조회에서도 같은 패킷을 확인한다. 종전 dev32의 불투명 로그 Job은
새 규격의 검증을 받은 것으로 소급 표시하지 않고 기존 결과 그대로 읽는다.
[HDR/LFM 안내](camera-platform-hdr-lfm-mode-readiness.md)에 패킷 필드와 경계를
명시했다. 로그 자체가 진짜 계측기에서 나왔는지, 두 자료가 독립적인지,
실제 픽셀이 공간 정합되는지는 확인되지 않았다. 비교는 여전히
`unregistered_global_means_only`이고 제품 판정은 `inconclusive`다.

## 아직 열리지 않은 제품 판정

기존 합성 인지 fixture의 mAP는 annotation의 독립 검증이나 제품 장면 분포를
대신하지 않는다. 고급 ISP에는 저장된 RAW 재처리와 source-disjoint 보류자료
평가, 등록 시점이 뒤인 prospective 선택 경로까지 있다. 다만 현재 선택 경로가
보류자료 접근을 격리·블라인드로 강제하지 않으며 물리 카메라에서 보정/독립
검증한 성능도 아니다. 우선 다음 작업은
1) p0014 또는 다른 실제 조합의 출처 확인된 Sensor 각도 응답, 2) 동일 각도
sampling을 유지한 충분한 장면 halo와 픽셀 적분 기록을 가진 MTF 재실행,
3) 실제 계측 패킷·독립 기준율의 원본 검토와 spatial registration이다.
이 세 근거가 없어도 연구 계산은 가능하지만 제품 순위를 승인하지 않는다.
