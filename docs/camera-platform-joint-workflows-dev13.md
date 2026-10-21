# Lens × Sensor 결합 작업 · `0.16.2.dev13` 개발판

이 버전은 [dev12의 결합 RAW·HDR/LFM·ISP·인지 입력·데이터셋 범위](camera-platform-joint-workflows-dev12.md)를 유지하고, `central_bounded`의 strict raytrace PSF 계산 시간을 줄입니다. 광학·센서 모델, 사용자 요청 schema와 저장 RAW 형식은 바꾸지 않았습니다. 검증한 설치 파일은 [dev13 wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev13-py3-none-any.whl)입니다.

기존 bounded 경로는 작은 광학 입력을 큰 0 패딩 배열로 확장한 뒤 각 PSF와 FFT convolution을 했습니다. 이제 0이 아닌 입력 영역만 full convolution하고 결과를 원래 `same` 출력 좌표에 놓습니다. 중심 시야 PSF는 방위각과 무관하므로 각도마다 같은 커널을 다시 생성하지 않습니다. 저장 결과의 `lens_model.psf_execution`에는 `convolution_strategy`, 실제 생성 커널 수와 nonzero 입력 convolution 수가 남습니다. 지원하지 않는 광학 모델에 bounded 모드를 지정하면 큰 PSF 계산 전에 거부합니다.

| Lens DB revision | 가상 창 | dev12 단독 소스 실행 | dev13 단독 소스 실행 | dev13 설치본 제출→완료 | RAW 수치 |
| --- | ---: | ---: | ---: | ---: | --- |
| P0014 | 8×8 | 약 50.9초 | 약 11.9초 | 약 12.9초 | dev12 bounded RAW 해시와 동일. 완료된 이전 전체 stack RAW는 없음 |
| P0248 | 8×8 | 약 18.6초 | 약 5.0초 | 약 6.3초 | dev12 bounded 및 이전 전체 stack RAW 해시와 동일 |
| P0014 | 32×32 | 비교 없음 | 약 3.8초 | 약 5.3초 | 같은 조건의 dev13 소스 RAW 해시와 동일 |

측정은 각 한 번의 로컬 실행이며 속도 분포나 실행시간 보장이 아닙니다. 32×32에서 시간이 더 짧은 것은 가상 창을 키우면서 장면 FOV와 광학 표본 간격이 달라져 PSF 리샘플 크기가 줄어든 결과입니다. 같은 촬영 조건에서 해상도만 키운 성능 곡선으로 해석하면 안 됩니다.

[단독 실행 근거](../reports/camera_platform/integrated-workflows-20260928/bounded-psf-sparse-source-dev13.json), [설치본 RAW·원본 dataset 근거](../reports/camera_platform/integrated-workflows-20260928/installed-bounded-psf-dev13.json), [설치본 ISP·인지 대조·cohort 근거](../reports/camera_platform/integrated-workflows-20260928/installed-bounded-downstream-dev13.json)를 분리해 보존했습니다. 단위·작업 계약 검사 39개가 통과했고, wheel 안의 소스 파일 1016개가 현재 패키지 소스와 일치했습니다. 설치본은 별도 child Worker에서 실행했습니다.

여전히 8–32 크기의 가상 창과 저장 `db_estimate_v1` 센서만 이 RAW 경로에서 지원합니다. 렌즈 절대 투과율·상대 조도는 unity 가정이고, CRA·픽셀 적분 PSF 및 실측 정확도는 미확립입니다. 인지 작업은 외부에서 제공한 box 예측을 대조하며 검출기를 실행하지 않습니다. HDR/LFM의 제품별 타이밍, 범용 고급 ISP, 원본 DB 조합의 동등 조건 화질 비교와 대표 native 해상도 성능도 아직 검증되지 않았습니다. 다음 통합 과제는 저장 Lens×Sensor 조합의 uniform/edge 반복 RAW에서 temporal 잡음과 공간 화질을 **가용성·비교 가능성 판정과 함께** 계산하는 것입니다. 단일 RAW의 공간 분산을 temporal SNR로 쓰거나 화각이 다른 렌즈를 그대로 순위화해서는 안 됩니다.
