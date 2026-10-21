# Lens × Sensor 공통 물체각 MTF 입력 지지 검사

2×2 Lens×Sensor 품질 Job의 `common_grid_object_angle_edge_mtf50`은 동일한
550 nm 점 중심 좌표 격자에서 계산하는 **시뮬레이션 민감도 지표**다. 각 카메라의
픽셀 면적과 PSF가 다른 것은 비교 대상 자체이며, 그 차이만으로 비교를 금지하지
않는다. 물리 MTF 비교에는 동일하게 점수화된 물체 좌표의 모든 입력이 각 시스템의
광학·픽셀·ISP 연산에서 유효했는지와 입력 해상도·경계·추정기 수렴 근거가 필요하다.

이번 `model_input_support_diagnostic`은 저장된 선형 ISP RGB, 유효 마스크,
파장별 기하 점 중심 지도와 정확한 점수화 ROI를 다시 사용한다. 각 시스템에서
점수화 좌표를 만드는 삼각형의 네이티브 픽셀을 찾아, 원본 프레임 경계까지의
최소 거리와 Gaussian 반앨리어싱 필터의 실제 유한 반경을 비교한다. 동일한
좌표를 `reflect`와 `constant` 경계 조건으로 다시 표본화해 최대 RGB 차이를
기록한다. 저장된 ISP profile과 실행 summary의 구현 ID·halo가 일치할 때만
해당 고정 ISP의 crop 경계 여유도 기록한다. 원본 장면의 파장별 **점 중심**
표본 한계까지 남은 각도도 계산한다.

[실제 dev29 저장 2×2 감사](../reports/camera_platform/mtf-support-20260928/saved-dev29-quality-audit.json)에서는
공통 16×16 격자 중 12×12가 점수화된다. 네 시스템의 점수화 표본에 대해
네이티브 프레임 최소 경계 여유는 각각 5·6·9·9픽셀, 반앨리어싱 반경은
3·3·1·0픽셀이었다. `reflect`와 `constant` 표본 차이는 모두 0이었다.
세 장면 파장 450/550/650 nm의 점 중심은 선언된 장면 표본 한계 안에 있었다.
저장 작업의 `isp_implementation`은 이전 코드의 잘못된 summary 경로 때문에
네 경우 모두 `null`이므로, 당시 ISP halo를 새 근거로 소급 인정하지 않는다.
새 작업에서는 실제 `summary.implementation.id`와
`crop_full_frame_comparison_halo_native_pixels`를 보존하고 profile에서
재계산한 값과 `get` 시점에 대조한다.

이 검사는 **최종 RGB 이후의 재표본화 경계**만 통과시켰다. 저장된 품질 Job에는
파장·시야별 광학 PSF 유효 입력 footprint와 픽셀 적분 kernel이 없고, 광학 입력
장면의 필요한 halo를 검증하지 못한다. 같은 장면을 더 촘촘히 생성하거나 광학
샘플링을 달리 한 독립 실행도 없어 MTF 추정기 수렴을 평가할 수 없다. 기존 렌즈
모델은 기하 PSF이며 회절·광량·CRA×픽셀 응답과 실물 정확도가 연결되지 않았다.
따라서 `physical_mtf_comparison_allowed`와 `product_ranking_allowed`는 계속
`false`다. 이 관문을 열려면 실제 광학·픽셀 커널과 소스 halo, 동일 물체 좌표
ROI의 독립 샘플링/추정기 수렴을 저장하고 재조회해야 한다.

이후 소스 RAW Job의 **실행된** bounded PSF metadata를 다시 추적했다. 새 품질
Job은 장면 샘플 수·OI 간격·PSF 파장축·전체 커널 크기·실제로 생성된 모든
파장/시야/방위 커널의 최대 비영 영역을 source-bound 조건으로 보존한다. 이
정보가 저장된 RAW Job의 resource preflight 및 장면 FOV와 일치할 때만,
점수화 ROI가 사용하는 네이티브 픽셀의 위치에서 모델상 PSF·반앨리어싱·
검증된 고정 ISP에 대한 **기록된 전역 최대 커널 기반 충분조건**을 계산한다. digital RAW가 거치는
mosaic 공간 적분의 세부 설정은 소스 RAW에 기록되지 않아 픽셀 kernel 범위를
포함한 완전한 충분조건은 만들 수 없다. 전역 최대 커널이 장면 안에 맞지 않으면
`recorded_global_upper_bound_fits_source_scene=false`지만, 이는 점수화 영역의
실제 지역 커널이 잘렸다는 증거가 아니다. 맞는 경우에도 지역·파장별 에너지와
픽셀 적분 지지가 없어 `scored_local_input_support_verified=null`로 둔다.
저장된 dev29 결과 자체를
변경하거나 과거 `null` ISP 구현 ID를 보정하지 않는다.

[소스 PSF 에너지 감사](../reports/camera_platform/mtf-support-20260928/saved-dev29-psf-scene-energy.json)의
실행 metadata에서는 두 렌즈의 최대 비영 커널이 각각 **849×849** 및
**709×709 OI 샘플**이었고 입력 장면은 각 **256×256 샘플**이었다. 점수화
픽셀에서 재계산한 보수적 전역 halo 여유는 네 조합 모두 음수다. 이는 전역
최대 커널만으로 충분한 장면 지지를 증명할 수 없다는 뜻이다. 어떤 파장/시야가
원인인지나 실제 점수화 픽셀의 에너지 손실을 이 값 혼자서 정량화하지 못한다.
별도 감사 스크립트가 해시로 고정된 원본 기하 PSF의 **중심
시야**를 실행 당시 OI 간격으로 다시 표본화했다. 무한 균일 입력을 가정한
중심 정사각 장면 창의 PSF 에너지 포함률은 450 nm에서 두 렌즈 각각
**26.14%, 26.12%**였고, 550/650 nm에서는 거의 100%였다. 이는 파장별
효과가 매우 다르다는 수치 진단이다. 실제 경사 에지의 입력, 시야·방위 보간,
센서 QE·ISP를 모두 다시 계산한 손실률이나 제품 MTF는 아니다.

현재 256 샘플 장면을 더 크게 캡처하는 것만으로는 충분하지 않다. 각도를
넓히면서 샘플 수를 유지하면 원래의 고주파 에지를 더 거칠게 표본화한다.
다음 촬영 조건은 동일한 각도 샘플 간격을 유지하면서 PSF 유효 에너지와
센서·ISP halo까지 덮는 장면 또는 명시된 무한 차트 경계 모델을 사용해야
한다. 그 다음 입력 크기·PSF 표본화·MTF 추정기를 독립적으로 바꾸어 같은
물체각 ROI에서 수렴을 확인해야 한다. 중심 PSF 에너지는 소스 해석의 수치
진단이며 회절·렌즈 광량·CRA×픽셀 결합이나 실측 검증의 대체물이 아니다.

## dev29 2×2 동일 각도 표본 간격 재촬영 사전 검사

[실행형 사전 검사 결과](../reports/camera_platform/mtf-support-20260928/saved-dev29-mtf-recapture-preflight.json)는
저장된 네 RAW와 품질 Job을 다시 검증한 뒤, 해시로 고정된 두 렌즈의 파장별
strict raytrace 왜곡 축을 읽는다. 센서 출력 **전체 32×32 프레임**에 네이티브
화소 중심, 기록된 모든 파장·시야·방위에서 생성된 최대 비영 PSF 반경,
점수화 재표본화의 유한 필터 반경을 더한다. 그 영역을 왜곡 역변환으로 원본
장면에 투영하고, bilinear 보간의 추가 1 OI 표본을 포함한다. 이는 중심 550 nm
좌표만 확장하는 근사가 아니다. 전역 최대 PSF를 쓰므로 통과 시 모델상 충분한
창 크기를 보수적으로 계산하지만, 통과하지 않는다고 점수화 ROI의 실제 지역
PSF 손실을 입증하는 것은 아니다.

기록된 전역 PSF·왜곡 범위만 넣었을 때 필요한 정사각 창은 L1S1 **976**,
L1S2 **988**, L2S1 **958**, L2S2 **967** 표본이다. 과거 RAW에는 실행된
`n_samples_per_pixel`, OCL group 설정과 PD/aperture 연산자가 없어서 픽셀
지지를 이 수치에 확정적으로 더할 수 없다. 현재 `sensor.py`의 *단일 subpixel·
OCL off* mosaic 경로를 조건부로 적용하면 QE 가중 current-density 평면에
`ceil(pixel pitch / OI spacing)` 크기의 Gaussian(이 사례에서는 L1 3×3,
L2 4×4)을 `same` convolution하고, bilinear 보간을 거쳐 CFA로 선택한다.
따라서 `same` convolution의 짝수 kernel 중심 치우침까지 포함한 보수적 추가
지지는 각각 2.0, 3.0 OI 표본이다. 이 가정하의 창은 각각
**980, 992, 965, 974** 표본이며 실행 증거로 인정하지 않는다.
`n_samples_per_pixel>1`이면 subpixel 위치, PD mask와 box 적분이 추가되고,
OCL group proxy가 켜졌다면 이웃 화소 범위도 다시 계산해야 한다.
과거 ISP 구현 ID가 `null`인 만큼 그 halo도 이 창의 확정 근거에 포함되지 않는다.
사전 검사 스크립트는 저장 Job의 코드 hash와 같은 현재 Sensor 설정 경로 및
해시 고정 Sensor DB snapshot을 영상 계산 없이 다시 구성했다. 네 경우 모두
`mosaic=true`, `n_samples_per_pixel=1`, OCL group 미설정, fill factor 1,
전면 PD, FDTD/TCAD 꺼짐으로 재구성됐다. 따라서 위 3×3/4×4는 임의의
픽셀 가정이 아닌 **동일 소스·설정의 재구성 결과**다. 다만 과거 실행 객체의
해당 필드가 RAW 결과에 저장되지는 않았으므로 직접 관측된 실행 metadata로
승격하지 않는다. 물리적인 픽셀 aperture 또는 수집 효율의 검증도 아니다.

네 경우에 공통으로 제안할 **1024×1024** 원본 장면의 수평 화각은
`2 atan((1024/256) tan(0.12°/2)) = 0.479997368132°`다. 이 식은 렌즈별
OI 표본 간격 0.287803257/0.202345479 µm와 물체각 tangent-plane 표본
간격을 그대로 유지한다. 제안 장면의 대각 화각 0.678815°는 두 해시 고정
렌즈의 `max_fov_deg` 안이다. 다만 1024는 기록되지 않은 픽셀·ISP 연산자를
검증한 값이 아니라 재촬영 후보값이다.

현재 `lens_sensor_raw_bridge._scene_samples()`는 명시 장면과 등록된 radiance
장면 모두 각 축 **최대 256** 표본으로 막는다. 실행형 사전 검사도 1024 요청을
`same_sampling_scene_unsupported`로 거절한다. 256에서 화각만 0.48°로
넓히면 원래보다 네 배 성긴 표본이 되므로 이 사전 검사를 통과하지 않는다.
저장된 256² RAW는 각각 114 커널과 432 sparse FFT convolution을 실행했고
약 5.6–7.1초가 걸렸다. 기존 bounded PSF 개발용 memory heuristic은
렌즈별 **1.252/1.009 GiB**지만 커널 중심 추정치이며 큰 장면의 peak RSS
상한은 아니다. 1024²에서는 세 밴드 float64 입력만 0.0234 GiB,
padding을 포함한 출력 배열은 렌즈별 0.1005/0.0890 GiB이며, 단일 full
convolution 실수 격자 하한은 0.0333/0.0295 GiB다. FFT 격자 면적은
기존보다 **2.47/2.65배**다. 복수 FFT 임시 배열과 실행시간은 별도 측정이
필요하므로 지금 수치로 3 GiB 정책 통과나 실행 시간을 보장하지 않는다.

재실행 순서는 다음과 같다.

1. RAW 원본 장면 상한을 확장하고 FFT/장면 배열까지 포함한 resource
   preflight를 실행한다. 동시에 최종 센서 객체에서 subpixel 수, pixel/PD
   기하, Gaussian kernel, interpolation, OCL group, CFA와 입력 OI 간격을
   RAW 결과에 저장한다.
2. 같은 Lens revision 2개·Sensor saved run 2개, 노출 0.004 s, 광량
   1000 cd/m², 32×32 출력, 450/550/650 nm, 동일 고정 ISP 조건으로 네
   RAW를 **새 Job**으로 계산한다. 입력만 1024²/0.479997368132°로 바꾼다.
   기존 Job은 그대로 보존한다.
3. 새 네 RAW의 파장·지역별 PSF 지지와 픽셀 적분 지지를 실제 점수화 ROI에
   대조하고 같은 공통 물체각 영역에서 품질 Job을 만든다. 1024와 더 넓은
   동일 표본 간격 입력을 비교해 경계 수렴을, 같은 물체각 영역의 더 촘촘한
   표본을 별도로 비교해 MTF 추정기 수렴을 확인한다.

사전 검사 실행 명령은 다음과 같다. `python`은 이 저장소 의존성을 설치한
Python 3.12 실행기를 뜻한다.

```sh
PYTHONPATH=src python -B \
  reports/camera_platform/mtf-support-20260928/verify-psf-scene-energy.py \
  --output reports/camera_platform/mtf-support-20260928/saved-dev29-psf-scene-energy.json \
  --preflight-output reports/camera_platform/mtf-support-20260928/saved-dev29-mtf-recapture-preflight.json
```

이 수치 검사는 PSF·왜곡 계산의 경계 문제를 구체화한 것이다. 기하 PSF에는
회절이 빠져 있고 렌즈 절대 광량·상대조도·CRA×픽셀 각도 응답과 실측 근거가
없다. 1024² 재촬영이 가능해져도 그 검증 전에는 물리 MTF 제품 순위를
열 수 없다.

현재 수정 범위의 회귀는 `test_lens_sensor_mtf_comparability.py`와
관련 공통각·품질 비교 테스트에서 43개 통과했다. 기존 dev29 품질 Job의
수치 지표는 보존된 NPZ에서 다시 계산해 일치함을 확인했다. 별도 복제한
Registry에서도 기존 v2 Job의 `get` 재조회가 성공했고 새 진단 필드는 없는
상태로 유지됐다.

## dev35: 입력 표본화 실험과 대형 장면 자원 관문

기존 v1–v6 RAW Job의 256×256 상한은 유지했다. 새 opt-in v7 요청은
`central_bounded_large_scene_preflight` 템플릿에서 1024×1024 장면을
선언할 수 있지만 현재 공유 Worker에는 독립 자식 프로세스의 RSS를 제한하고
감시하는 실행 경계가 없다. 이 프로세스에서 시험한 8 GiB
`RLIMIT_AS`·`RLIMIT_DATA` 설정은 실패하며, Darwin의 `RLIMIT_RSS`는
`RLIMIT_AS`의 별칭으로 강제 RSS 상한이 아니다. 따라서 v7 제출은 광학 실행 전에
`resource_blocked_process_guard_unavailable`로 거부한다. 이 사전 검사는
개발 상태의 단순 PSF 커널 추정에 31밴드 원본 D65 Scene, 3밴드 실행 Scene,
기하·마스크 평면, padded 출력, 4개 PSF 캐시, 실제 SciPy FFT 격자를 더한다.
이는 **peak RSS의 증명된 상한이 아닌 정책용 추정**이다.

[새 2×2 자원 사전 검사](../reports/camera_platform/mtf-support-20260928/dev35-large-scene-resource-preflight.json)는
L1 조합 3.994 GiB, L2 조합 3.668 GiB의 정책 추정과 FFT 격자
2156²/2000²를 기록했다. 추정치가 8 GiB 정책 한도보다 작더라도
프로세스 경계가 없으므로 네 경우 모두 실행을 거부했다. 기존 RAW와 품질
Job은 그대로 보존했고, 1024 RAW나 새 MTF 수치를 만들지 않았다.

`adaptive_lens_sensor_raw_preview()`는 별도 opt-in **256 이하 진단 경로**다.
저장 렌즈의 원본 PSF pitch와 저장 센서 pitch로 첫 장면 격자 *후보*를 고른
뒤, 동일한 물리 FOV·광량·노출·렌즈·센서에서 연속 tangent-plane 경사
에지를 각 해상도로 다시 생성한다. 0과 고정된 반 픽셀 각도 이동을 각각
noise-off 촬영하고 입력 간격을 절반으로 줄이며 readout 전 평균 광전자,
RAW mosaic의 ISO 경사 에지 MTF50 **수치 proxy**, subpixel 변화량을 선언한
허용 오차와 비교한다. 초기 격자 표본 수 후보가 256을 넘으면 32/64/128/256
같은 *후보 미달 진단 사다리*를 실행하고 그 사실을 표시한다. 두 단계가
수렴하기 전에 256 한도에 닿으면 `inconclusive_resource_limit`이다.
평탄 장면이나 MTF50=0을 수렴으로 인정하지 않는다. 이전 256 raster를
확대해 새 물리 장면이라 주장하지 않으며, 외부 raster 입력의 반복 sampling은
선언된 raster에 대해서만 판단한다. 이 controller의 허용 오차는 연구용
기본값이며 호출자가 변경할 수 있고 결과에 보존된다.

[실제 적응형 1조합 실행](../reports/camera_platform/mtf-support-20260928/dev35-real-adaptive-lens-sensor-ladder.json)은
같은 연속 에지·0.12° FOV에서 **32→64→128→256** 장면 격자를
각각 0/고정 반 픽셀 offset으로 계산했다. 128→256에서도 readout 전
평균 광전자 상대 변화 **12.52%**, shift 응답 변화 **8.12%**로 선언한
1%/2% 허용치를 넘었다. 16×16 Bayer mosaic의 ISO12233 MTF50은
0.2→0.4→0.6/0.4 cy/pixel처럼 거칠게 양자화된다. 128→256의 이 proxy
변화가 0이어도 독립적인 edge/시스템 MTF 수렴 증거가 아니다. 256 상한에 닿아 결과는
`inconclusive_resource_limit`이며 저장 PSF·Scene halo 관문도 미통과다.

새로 opt-in 기록하는 RAW의 `scene.sampling_evidence`는 원본 저장 PSF,
광학 convolution OI, 네이티브 센서의 **세 pitch**를 분리한다. 실행 센서
객체에서 mosaic, subpixel 수, Gaussian kernel, bilinear zero 보간,
CFA 선택 순서, fill factor/PD area, OCL group을 기록한다. 광학 PSF는
픽셀 적분 전 모델이고 pixel aperture를 다시 포함하지 않는다. 중심 시야의
450/550/650 nm 저장 PSF와 동일한 strict solver 격자 재표본 PSF에서
centroid, RMS 폭, 1D MTF50 및 **연속 point density라고 가정했을 때의**
unit-sum 이전 quadrature 비율을 기록한다. 저장값은 ray histogram이므로
이 비율의 물리 적용성은 알 수 없다. unit-sum 1은 DC 정규화일 뿐 에너지
보존 검증이 아니다.

[dev35 소형 실제 RAW 실행](../reports/camera_platform/mtf-support-20260928/dev35-executed-small-raw-sampling-evidence.json)은
64² 연속 에지 장면과 8² native 출력에서 이 기록 경로가 동작함을 확인했다.
원본 PSF pitch 0.612884917 µm, 실행 OI pitch 1.151213027 µm,
native pixel pitch 0.61 µm와 mosaic Gaussian 1×1을 분리 보존했다.
중심 450 nm PSF의 64² 창 포함률은 29.43%로, 이 실행도 충분한 halo나
MTF 근거가 아니다. 보고서에는 dev35 버전과 소스 fingerprint hash를 담았다.

[비대칭 PSF 반례](../reports/camera_platform/mtf-support-20260928/psf_resampling_counterexample.json)는
1 µm OI에서 정규화 후 DC=1인데도 centroid가 기준 0.35 µm 대신
0.0013 µm가 될 수 있고, 45° 회전 시 저장창 안에 남은 질량이
58.06%에 그친다는 것을 재현한다. 저장 Lens PSF 자체는 광선 교점을
bilinear로 투입한 **기하 ray histogram**이며 연속 irradiance density나
pixel-integrated PSF로 규정되지 않았다. 현재 NPZ에는 원래 광선별 교점도
저장되지 않았다. 원본·OI·절반 pitch의 보간 결과가 서로 가까워도 같은
histogram을 재구성한 결과일 뿐 광학 진실을 증명하지 못한다. 다음 관문은
해시로 고정한 처방과 빌드 조건에서 동일 광선을 다시 추적하여 교점·성공
마스크를 보존하고, 원본/목표 pitch로 독립 re-histogram한 centroid·encircled
energy·OTF·남은 질량을 파장·시야·방위별로 대조하는 것이다.

이 관문의 **선택 조건 진단**으로 P0014 고정 revision의 광선을 조건마다 한 번씩 재추적하고,
동일한 성공 교점을 원본 0.612884917 µm와 목표 0.306442459 µm 격자에 각각
투입했다. [중심 550 nm](../reports/camera_platform/mtf-support-20260928/p0014-trace-once-half-pitch.json),
[중심 450 nm](../reports/camera_platform/mtf-support-20260928/p0014-trace-once-central-450-half-pitch.json),
[3번 시야·550 nm·45°](../reports/camera_platform/mtf-support-20260928/p0014-trace-once-field3-550-az45-half-pitch.json)에서
원본 재구성은 저장 histogram과 최대 셀 오차 각각 1.31×10⁻¹⁰,
1.45×10⁻¹¹, 2.35×10⁻⁸로 일치했고, 두 격자의 추적 광선 유지율은 모두
1이었다. 반면 목표 격자의 직접 재히스토그램과 현재 strict 보간 결과 사이
L1 질량 차이는 각각 0.586, 1.335, 0.703이었다. 주변부 45° 회전의
저장창 잔류 질량은 재정규화 전 0.9949였다. 이는 **기하 광선의 선택된
파장·시야·방위만** 비교한 독립 PSF 표본화 기준이며, 보고서의 production
PSF 재표본화 적격성과 물리 MTF 비교 관문은 모두 `false`다. 아직 전 시야·
전 파장·회절·렌즈 광량·픽셀 개구·Scene halo를 검증하지 않았다.

따라서 장면 표본화, 장면 halo, PSF 재표본화와 전체 시야 광학 정확도는
각각 다른 검증축이다. 256 이하의 수치 반복이 수렴해도 저장된 450 nm
중심 PSF의 옛 입력창 포함률 약 26%, 알 수 없는 지역 halo·ISP 영향,
회절·광량·CRA×픽셀 각도 응답이 남는다. 물리 MTF 비교와 제품 순위는
계속 금지한다.

## dev36: 1024 작업의 프로세스 메모리 경계

[작은 자식 프로세스 fault injection](../reports/camera_platform/mtf-support-20260928/process-guard-fault-injection-20260928.json)은
부모 감시기가 32 MiB를 넘은 직접 자식의 RSS를 발견해 종료하고, 50 ms
시간 초과도 종료하며, 격리된 임시 상태를 제거한 것을 기록한다. 이는
**고장 억제 실험**이다. polling 사이의 순간 할당은 제어하지 못하고 이
macOS sandbox에서는 전역 PID 열거용 `sysctl`이 거부돼 자손 프로세스
RSS도 조회할 수 없다. `RLIMIT_AS` 256 MiB 설정 시험은 `ValueError`로
실패했다. 추가로 격리된 작은 Python 자식에서 8 GiB AS·DATA 설정은
같은 오류로 실패하고 512 GiB 설정은 성공했다. 가상 주소 공간 예약량이
약 420 GB인 이 실행 환경에서의 작은 상한 실패를 macOS가 모든
`setrlimit`을 금지한다는 증거로 해석하면 안 된다. `RLIMIT_RSS`는 Darwin에서
`RLIMIT_AS`의 별칭이며, 허용되는 큰 주소 공간 상한도 8 GiB RSS 제한은
아니다. 해당 결과는 1024×1024 Scene의 강제 메모리 경계가 아니다.

따라서 v7 preflight는 배열·FFT 추정치가 8 GiB 아래여도
`resource_blocked_process_guard_unavailable`을 반환하고, 제출 관문은
결과 객체의 `within_guarded_process_budget` 값을 임의로 바꾸더라도
`resource_blocked_large_scene_executor_unavailable`로 거부한다. 독립된
하드 메모리 격리 및 실행 결과의 안전한 승격 경로가 검증되기 전에는
1024 Lens×Sensor RAW를 공유 Worker에서 실행하지 않는다.

## dev36: 고정 광선의 목표 격자 직접 재히스토그램

렌즈 DB의 v2 기하 PSF는 저장 격자의 ray histogram이며, 기존 strict 경로는
이를 Scene/OI 격자로 선형 보간한다. 새 opt-in RAW `v8`은 해시로 고정된
처방·PSF 자산과 빌드 조건에서 **저장된 모든 시야×파장 노드**의 광선을 다시
추적한다. 성공한 동공 광선 마스크, chief ray, 원본 격자 histogram을 먼저
대조한 뒤 같은 교점을 선언한 OI pitch에 직접 투입한다. 원본 자산 SHA와
새 커널 배열·도출 설정 SHA를 별도로 보존한다. 광학 PSF에는 픽셀 개구가
포함되지 않는다. 현재 저장 센서의 단일 subpixel mosaic 경로는 Gaussian
전처리·bilinear 표본화 뒤 `fill_factor × pixel_area` 근사를 한 번 적용한다.
연속적인 PD 개구 적분이나 시스템 MTF의 물리 정확도는 검증되지 않았다.

API/SDK에서는 `lens_sensor_raw_template(scope=
"central_bounded_direct_geometric_psf_diagnostic")`로 v8 계약을 받는다.
이는 명시적인 공통 FOV·Scene 표본 수, 저장 센서 revision, 근사 렌즈 광량
수락을 요구한다. P0014·16×16 RAW의 0.12° 장면에서 128² 후보는 OI
0.5756 µm, 목표 PSF 배열 예상 34.24 MiB로 진단 예산 안에 있다. 같은
FOV의 256² 후보는 0.2878 µm, 보수적인 float64 배열 예상 136.22 MiB로
현재 192 MiB 도출 예산과 bounded convolution 사전 검사에 들어온다.
이는 **작업 자원 정책**이지 256²가 충분한 물리 해상도라는 뜻이 아니다.
그보다 큰 입력의 수치 수렴을 주장하려면 더 큰 작업을 격리·감시할 수 있는
실행기가 필요하다.

v8의 직접 재히스토그램은 같은 strict solver에서 기하 PSF **입력 방식의
영향**을 비교하는 수치 진단이다. 기존 strict solver가 여전히 off-axis 커널을 회전·보간하고,
유한 동공 표본 수·광원 대역·회절·렌즈 광량·CRA×픽셀 응답·장면 halo의
독립 검증은 남는다. 45° 회전 전후의 격자 합 비율은 보간 때문에 1을
넘을 수도 있으므로 물리적 광선 유지율로 해석하지 않는다. 선택한 45°
광선 재히스토그램 비교는 회전 오차를 보여주는 진단이며 전체 방위각의
적격성은 아니다. v8 작업에는 soft trace 시간과 커널 바이트 예산은 있지만
hard RSS/시간 경계는 없다. 결과의 `physical_mtf_comparison_allowed`와
`product_ranking_allowed`는 계속 `false`다.

[dev36 저장 RAW 비교](../reports/camera_platform/mtf-support-20260928/dev36-direct-rehistogram-vs-stored-raw.json)는
같은 P0014 렌즈 revision·DEF-2512-801 저장 센서·0.12°/128² 장면·노출·seed에서
기존 v5와 v8을 실제 Worker 작업으로 각각 계산하고, 새 Registry 인스턴스에서
원본 RAW artifact를 다시 읽었다. v8은 15개 시야×파장 노드에서 원본 histogram의
최대 셀 오차 7.93×10⁻⁸, 최대 L1 오차 1.36×10⁻⁵를 확인했다. 원본 PSF
pitch 0.612885 µm, 도출 PSF/OI pitch 0.575607 µm, 센서 pitch 0.61 µm가
서로 구분돼 저장됐다. 16² RAW 중 30픽셀이 달랐고 최대 차이는 1 DN,
평균 광전자 변화는 −0.0562%였다. 이것은 **PSF 입력 경로 변경의 민감도**이지
물리 오차나 화질 개선량이 아니다. 선택된 45° 주변부 노드에서 동일 광선의
직접 회전 재히스토그램과 기존 v5 회전 결과의 L1은 0.346, v8 회전 결과는
0.218로, v8에도 남은 회전 오차가 있다.

[dev36 실제 적응형 v8 반복](../reports/camera_platform/mtf-support-20260928/dev36-adaptive-direct-psf-ladder-256.json)은
같은 연속 경사 에지를 매번 다시 생성해 32→64→128→256² 입력과 두
subpixel 위치를 계산했다. 마지막 128→256에서도 readout 전 광전자 변화
**14.23%**, 위치 이동 응답 변화 **9.17%**로 선언한 1%/2% 한도를
넘었다. 16² Bayer mosaic의 MTF50 수치 proxy는 마지막 두 격자에서
같았지만 양자화가 커 독립 MTF 수렴 증거가 아니다. 결과는
`inconclusive_resource_limit`이며, 256² 계산 가능과 256² 정확도 충분을
혼동하지 않아야 한다. 128² v8 실행의 중심 450 nm PSF는 같은 Scene
창 안에 정규화 질량의 약 **26.18%**만 들어와 Scene halo도 큰 미해결
요인이다. 다음에는 고정 pitch에서 analytic Scene의 물리 주변 영역을
늘리는 검사를 격자 밀도 증가와 분리해야 한다.

## dev37: 화각과 고해상도 입력의 실행 경계

이번 호스트는 Apple M4 Mac mini, 통합 메모리 **32 GB**다. 기존
`CENTRAL_PSF_STACK_BUDGET_BYTES = 3 GiB`는 개발 중 관찰에 근거한
bounded PSF 작업 정책이고, 대형 장면의 `8 GiB`는 opt-in 작업의 RSS
추정치에 적용한 정책이다. 어느 값도 CPU/운영체제가 강제하는 메모리
상한이 아니다. dev35의 1024² 자원 추정 **3.994/3.668 GiB**는
실제 peak RSS가 아니라 배열·FFT 기반 입장 검사 수치다. 8 GiB 아래라도
프로세스 단위 hard RSS 격리와 자손 감시가 검증되지 않아 공유 Worker의
1024² Job은 계속 거부한다. 남은 가용 메모리는 다른 작업에 따라 변하므로
32 GB 전부를 한 Job에 할당할 수도 없다.

1024²/0.479997° 제안은 256²/0.12°와 같은 영상면 표본 간격을
유지하면서 **Scene의 물리적 주변 영역을 넓히는 후보**다. 광학 표본을
네 배 촘촘하게 만드는 조건이 아니다. P0014의 이 간격에서 전체 표본화된
PSF는 약 1091², 중심에서 가장자리까지 545 OI 표본이므로 중심 RAW
영역에 필요한 원본 Scene halo가 1024 격자를 넘을 수도 있다. 한 선택
조건에서 PSF 질량 대부분이 창에 담겼다는 사실과 전 파장·시야·방위
커널의 지지를 보장한다는 주장은 구분해야 한다.

실제 저장 P0014×DEF-2512-801 연산자 기록을 재구성한 **계획 전용**
검사에서는 다른 조건인 1024²/0.959978°를 사용했다. 이 조건은 실행
이력이 있는 dev36 OI 간격 **0.575607 µm**를 유지하므로 표본화 PSF는
547²이고, 필요한 원본 영역은 `[219,805)²`로 계산됐다. 이는 1024
입력에서 원천 구역을 산술적으로 찾은 결과이며 RAW를 실행하지 않았다.
원래 dev29의 더 촘촘한 **0.287803 µm** 간격에 대한 실행 센서 연산자
기록이 없으므로, 위 계획을 dev29 재촬영이나 MTF 수렴의 근거로
옮길 수 없다. 같은 1024라는 배열 크기만으로 화각·해상도를 함께
충족했다고 판단하면 안 된다.

새 읽기 전용 `lens-sensor-fov/inspect` API와 SDK는 저장된 Lens revision과
Sensor run을 고정하고, **별도로 선언한 전체 active mode**의 외곽 네
모서리가 파장별 왜곡 역변환 후 저장 geometry·PSF·상대조도 field 안에
있는지 검사한다. DB의 32×32 계산 crop이나 명목 MP 수에서 전체 active
mode를 추론하지 않는다. 현재 저장 DEF-2512-801만으로는 active 행·열,
센서 중심과 광축의 오프셋이 없어 `not_evaluable`이다. 사용자가 기하를
선언해 통과하더라도 이는 **저장된 기하 모델의 범위**일 뿐 실제 렌즈
image circle, 광량, MTF, CRA×픽셀 응답 또는 제품 적격 판정은 아니다.

광학 단계에는 전역 좌표에서 PSF 시야 위치를 고정하고 소스 타일들의
기여를 요청 ROI에 합산하는 계산을 추가했다. 저장 P0014 450 nm 기하
PSF로 작은 입력의 전체 광학 결과와 ROI 타일 결과가 최대 절대 오차
3.5×10⁻¹⁸ 이내에서 일치했다. 이 검사는 **이미 왜곡된 전체 광학 입력을
메모리에 보유**하므로 1024² Scene이나 전체 RAW의 메모리 절약 증거는
아니다. 별도 중심 OI crop 사전 검사는 고정 노출·지원되는 센서 연산자,
전역 중심, 홀짝 격자, Gaussian·bilinear 의존 영역을 요구한다. 저장
DEF-2512-801 저장 센서 구성의 16×16 RAW에서는 전체 OI와 안전한 중심 crop의
정수 RAW가 일치했다. 이는 이미 계산된 OI의 **센서 단계** 동등성이다.

`plan_strict_scene_tile()`은 native RAW core에서 ISP·센서·전체 PSF
범위를 역으로 확장하고, 파장별 strict 왜곡을 거쳐 필요한 원본 Scene
사각형과 물체각·정책 메모리를 반환한다. 결과의
`execution_qualified=false`는 의도적이다. 이 dev37 시점에는 광학 ROI
함수와 별개로 전역 좌표를 보존한 Scene 생성·왜곡·센서·ISP 타일 실행기,
충분한 물리 주변 장면, hard 메모리 격리, 동일 격자의 전체/타일 RAW
대조가 필요했다. 4096²는 계획 계산일 뿐 실행 가능한 장면 해상도로
표시하지 않았다. 화각 범위 판정, RAW 수치 동등성, 입력 해상도 수렴,
물리 MTF 적격성은 서로 다른 검증 단계다.

## dev38: 메모리 경계의 의미와 타일식 진단의 범위

이 호스트의 물리 RAM은 **32 GiB**다. 다음 숫자는 하드웨어의 절대
해상도 한계가 아니라 코드가 선언한 **작업 정책**이다.

| 값 | 현재 의미 |
| --- | --- |
| 기존 RAW Job의 축별 256 표본 | 해당 API·진단 경로의 입력 상한. 256에서 광학·RAW 수렴이 끝났다는 뜻이 아니다. |
| 대형 요청의 축별 1024 표본 | v7 opt-in 계약과 사전 검사의 입력 상한. 공유 Worker에서는 실행이 거부된다. |
| 3 GiB / 8 GiB | 각각 bounded PSF와 대형 작업에 적용한 추정·입장 정책. 검증된 peak RSS나 OS 강제 상한이 아니다. |
| 공급자 단일 타일 1,048,576 셀 | 분석용 Scene/왜곡 공급자가 한 번에 생성할 수 있는 셀의 개발자 가드. 전체 Scene 크기나 최종 RAW 정확도 한계가 아니다. |

macOS에서 이 프로세스의 `RLIMIT_RSS`는 `RLIMIT_AS`의 별칭이고, 8 GiB
주소 공간 상한을 설정하는 시험은 실패했다. 이는 Python 프로세스가
이미 약 420 GiB의 **가상 주소 공간**을 예약한 것과 양립한다. 512 GiB
상한 설정은 성공했으므로 “샌드박스가 모든 메모리 제한을 금지한다”는
표현도 틀리다. 512 GiB 주소 공간 제한은 8 GiB **실제 RAM** 제한이
아니다. 부모의 RSS polling은 관측 사이의 할당 급증을 막지 못하며,
여기서는 자손 프로세스 RSS를 전부 조사할 수도 없다.
새 타일 공급자는 optics 상태를 복사해 고정하므로 PSF LUT 한 벌의 메모리를
추가로 사용한다. 따라서 이전 전체 배열 경로의 3.994/3.668 GiB 추정치를
새 경로의 peak RSS 예측으로 옮길 수도 없다.

새 분석용 경로는 연속 slanted-edge Scene을 전역 좌표에서 필요한 부분만
생성하고, 저장 strict raytrace 왜곡·광량을 타일에 적용한 뒤, PSF
convolution의 중심 광학 ROI를 합산하고, 지원되는 고정 노출 센서의
pixel/QE/readout으로 RAW를 계산한다. 저장 P0014 렌즈와 DEF-2512-801
센서의 **32² Scene → 16² RAW** 한 조건에서 전체 `oi_compute()`·
`sensor_compute()`와 비교했고, 같은 seed의 정수 RAW가 일치했다.
광학 ROI와 평균 광전자는 부동소수점 허용 오차에서 일치했다. 이 검사의
전체 계산 기준은 당연히 32² 배열을 만들었으며, 타일 계산 자체의
1024² peak RSS를 측정하거나 full-frame FOV를 검증한 증거는 아니다.
공급자의 Scene ID는 이 분석 입력의 해시이고, 일반 PSF callback의
`source_identity`는 호출자가 선언하는 문자열이다. 계산 범위가 다른
두 신원을 혼동하지 않으며, PSF·geometry 서명 검사는 전체 렌즈 광량
응답의 검증으로 표시하지 않는다.
진단 API는 `make_analytic_strict_geometry_provider()` →
`AnalyticCaptureResourcePolicy(...)` → `preflight_streamed_analytic_capture()`
→ `capture_streamed_analytic_center_roi()` 순서다. 실행 전 사전 검사는
PSF 목표 격자, 공급 타일·왜곡 질의, 센서 중심 halo, 복사한 optics 배열
및 FFT의 정책 추정치를 확인한다. `ready_under_declared_policy`는
**그 정책 안에서 진단 실행을 시도할 수 있다**는 뜻이며, hard RSS 격리나
제품 설계 적격 상태를 뜻하지 않는다.

이 경로는 현재 **분석용 edge와 중심 ROI**에만 적용된다. 유한 Scene
경계의 0 외삽, PSF halo, 다른 Scene 종류, 전체 active sensor mode,
CRA×픽셀 각도 응답, 회절·광량의 물리 정확도, ISP·MTF 적격성은
별도 확인이 필요하다. Scene 표본 수가 늘어도 동일한 광학 pitch에서
주변 화각이 넓어지는 경우와 같은 화각에서 표본 간격이 작아지는 경우는
계산 부하와 PSF 크기가 다르다. 현재 근거로 “최대 N×N까지 정확하다”는
단일 숫자를 제시할 수 없다. 저장 P0014×DEF-2512-801의 실행 OI 간격
0.575607 µm를 고정한 **계획 전용** 검사에서는 중심 4² native RAW core에
대해 1024²/0.959978°와 2048²/1.919821° 모두 PSF 547²와 원본
586² 영역을 요구했다. 국부 타일 정책 추정은 두 경우 약 1.31 GiB로
같지만 peak RSS 측정이나 2048² 촬영 가능성의 증거가 아니다. 반대로
같은 FOV에서 격자만 촘촘하게 하면 PSF의 표본 수와 메모리 비용도 커진다.

[독립 자식 프로세스의 중심 ROI 진단](../reports/camera_platform/mtf-support-20260928/dev38-streamed-analytic-central-roi-observation.json)에서는
같은 0.575607 µm 간격의 **1024²/0.959978°** 및
**2048²/1.919821°** 분석 장면을 각각 128² source tile로 계산했다.
두 경우 모두 중심 36² 광학 ROI에서 저장 16² RAW가 생성됐고,
각각 36개 타일, 약 **7.1/8.3초**의 부모 관측 시간, 직접 자식의
최대 **440.0/437.8 MB 관측 RSS**를 기록했다. 이는 20 ms polling의
최댓값으로 실제 순간 peak의 증명된 상한이 아니다. 두 실행 모두
같은 중심 RAW 합계를 출력했지만 배열 전체의 상호 동등성이나 해당
고해상도 격자의 전체 기준 계산과의 수치 동등성을 입증하지는 않았다.
이 관측은 **중심 ROI만 처리하는 경로에서 1024·2048 선언 격자가 이
호스트의 하드웨어 장벽에 걸리지 않았음**을 보여준다. 전체 Scene/OI
영상의 1024²/2048² 처리 능력과 제품 FOV·MTF는 여전히 미검증이다.
같은 물리 Scene의 해상도 반복과 주변 Scene halo 확인도 분리해야 한다.

## dev39: 가상 전역 격자와 주변부 타일의 현재 계약

dev38의 중심 ROI 진단 뒤에, 저장된 strict PSF 전체 지지 범위와 파장별
역왜곡이 필요로 하는 **원본 Scene 범위**를 계산하는 독립 사전 검사를
추가했다. `preflight_strict_optical_source_halo()`는 전역 좌표의 중심 또는
주변부 광학 ROI를 받고, PSF envelope와 각 파장의 Scene 질의 범위가
선언된 유한 Scene 안에 드는지 반환한다. 2048²/0.575606513 µm P0014
모델에서 중심 36² ROI와 열 중심 1536의 36² ROI는 수치 경계 안에,
열 중심 1792의 ROI는 0 외삽 경계에 닿는 것으로 나왔다. 열 1536의
영상면 반경은 약 0.295 mm로 첫 저장 PSF field 3.415 mm보다 훨씬
작다. 이 검사를 주변 화각 전체의 평가로 해석하면 안 된다. 요청
파장과 PSF·상대조도 field가 저장 기하 LUT 범위에 드는지도 확인한다.

`preflight_fixed_sensor_tile_mean_electrons()`와
`compute_fixed_sensor_tile_mean_electrons()`는 전역 OI·센서 원점을
유지하고, ROI 밖에서 필요한 Gaussian·선형 보간 halo와 전역 CFA 위상을
계산한다. 전역 크기가 커도 좌표 배열은 요청한 **센서 타일 축 길이**에
비례한다. 작은 합성 전체 OI 기준과 n=1·3·5 subpixel 설정에서 비교했다.
이를 광학 타일과 묶는 `preflight_streamed_analytic_offaxis_mean()` →
`compute_streamed_analytic_offaxis_mean()`는 별도의 정책 안에서 **잡음 전
평균 광전자 타일**을 반환한다. 작은 합성 장면에서 기존 전체
`oi_compute()`·`sensor_compute_noise_free()`의 해당 주변부 crop과 비교했다.
정수 RAW의 seed별 동일성, 전체 센서 프레임, ISP·MTF 판정은 여기서
수행하지 않는다. 큰 전역 격자는 실제 배열이 아니라 타일의 좌표계다.

분석용 연속 Scene 경로에서 초기 개발자가 둔 수평 FOV 30°와 edge 위치
±1° 제한은 제거했다. 기하식의 유효 범위인 180° 미만을 요구하며,
선택한 strict 렌즈의 **대각 시야 상한**을 별도로 적용한다. 기존
v1–v7 RAW Job의 256/1024 입력 상한과 이 진단 경로는 다른 계약이다.
저장 P0014 렌즈의 최대 ideal field 반경 13.66 mm에 맞춘 가상의 4:3
직사각형을 같은 0.575606513 µm 간격으로 단순 표본화하면 약
28,478×37,971 셀이다. 3파장 float64 **Scene 한 벌만 약 24.17 GiB**이며,
광학·센서 중간 배열은 별도로 필요하다. 이는 32 GiB 호스트에서 전체
배열 방식이 실용적이지 않다는 규모 계산이지, 그 크기의 렌즈×센서
영상을 실행했다는 뜻이 아니다. 타일식 국부 계산의 현재 한계는 숫자
하나가 아니라 PSF 크기, 요구 halo, 요청 ROI, 타일 정책 및 실행 관측에
따라 달라진다.

`preflight_fullfield_study()`는 실제 active 행·열, pixel pitch, 광축
오프셋, 센서 회전·CFA 원점, 저장 처방의 영상면과의 축방향 결합을
해시로 고정한 선언 파일에서 요구한다. 90°·270° 센서 회전에서는
렌즈 좌표계의 active 행·열 크기를 바꿔 검사한다. DEF-2512-801의
저장 16×16 값은 제품 active 크기가 아니라 PDAF unit-cell 자료이며,
저장 32² 계산 crop도 제품 전체가 아니다. 따라서 선언이 없는 이 DB
조합의 전체 FOV 결과는 `not_evaluable`이다. 선언이 저장 기하 field
안에 들어와도 결과는 `planning_only`이며 제품 FOV·MTF 적격이나
전체 프레임 실행을 허용하지 않는다. 저장 PSF는 기하 raytrace이며
회절·실제 광량·CRA×픽셀 각도 응답의 물리 검증도 여전히 필요하다.

[P0014 첫 저장 주변부 field의 독립 광학 진단](../reports/camera_platform/mtf-support-20260928/p0014-first-offaxis-streamed-optical-roi-observation.json)은
전역 **2048×14000** 분석 Scene을 실제 배열로 만들지 않고, 저장
550 nm field의 ideal 3.415 mm·distorted 3.259802 mm 위치에 36²
광학 ROI를 계산했다. 수평 FOV는 13.068°, edge 위치는 5.544696°,
입력 간격은 0.575606513 µm다. 저장 PSF의 547² 전체 지지와 세
파장의 역왜곡 원본 범위는 이 선언 Scene 안에 있었고, 128² source
tile **36개**를 처리했다. 독립 자식 프로세스는 약 **8.0초**에 정상
종료했고, 20 ms 간격으로 읽은 직접 자식 RSS 최댓값은 **441.9 MB**다.
RSS의 순간 peak 상한은 아니며 자손 프로세스 메모리도 포함하지 않는다.
이는 특정 저장 기하 PSF의 주변부 **광학 타일** 한 장을 이 호스트에서
실행했다는 근거다. DEF 센서와의 결합 RAW, 첫 field의 실제 제품
센서 포함 여부, 회절·광량·물리 MTF의 검증 근거는 아니다.

`_strict_raytrace_apply_psf_bounded_roi_provider()`도 전역 Scene 전체
타일을 순회하지 않고 요청 ROI의 PSF padding과 만날 수 있는 타일
index만 순회하도록 바꿨다. 전역 격자가 매우 커져도 이 단계의 순회
비용은 실제 후보 타일 수에 비례한다. 이는 알고리즘의 비용 범위 개선이며
타일 하나의 PSF FFT 메모리나 렌즈 모델 정확도를 낮추지는 않는다.

## dev40: 메모리 정책과 수치 해상도 진단

가상 격자의 전역 좌표축을 먼저 `linspace`로 만들고 일부를 잘라 쓰던
잔여 경로를 수정했다. strict PSF 위치별 convolution과 분석 Scene의
왜곡·cos⁴ 좌표는 이제 필요한 타일 길이의 축만 계산한다. 원래 전역
좌표값 및 위아래 반전의 작은 격자 수치 동등성과 1천만×1천만 **가상**
격자의 작은 ROI에서 전역 축 배열을 만들지 않는 회귀 검사를 통과했다.
이 검사는 1천만×1천만 영상이나 센서 RAW를 실행했다는 뜻이 아니다.

### 메모리 경계의 의미

위 dev38 표의 256/1024 표본과 3/8 GiB 숫자는 하드웨어가 정한 최대
Scene 해상도가 아니다. 특히 새 `AnalyticCaptureResourcePolicy`의 8 GiB는
호출자가 선언할 수 있는 **추정 작업량**의 상한이다. 이 사전 검사에
통과해도 프로세스 최대 RSS나 실행 성공은 보장되지 않는다. 반대로
정책 거부도 하드웨어에서 계산할 수 없다는 판정은 아니다.

위의 P0014 광학 타일은 전역 2048×14000 **좌표계**에서 36×36 ROI만 계산했고,
직접 자식 RSS의 관측 최댓값은 약 442 MB였다. 같은 격자로 저장 렌즈의
가상 4:3 시야 전체를 3파장 float64 배열로 만들면 Scene 한 벌만 약
24.17 GiB다. 다른 중간 배열·FFT·센서·readout·운영체제 메모리를 더하면
전체 배열 방식은 이 호스트에서 실용적이지 않다. 이 수치는 해당 모델과
격자에 대한 규모 추정이지 제품 센서의 active FOV 계산 결과가 아니다.

기하학적 FOV 범위 자체는 센서 active 치수·장착 관계와 렌즈 왜곡
기하로 판단하므로 고해상도 Scene이 필수인 것은 아니다. 고해상도 Scene이
필요한 질문은 그 FOV 안의 위치별 선예도·작은 물체·aliasing 같은 영상
성능이다. 현재 저장 센서에는 제품 active mode와 장착 조건이 없어
기하학적 전체 FOV도 아직 판정할 수 없다.

따라서 영상 성능용 해상도 결정은 고정된 256/1024 상한을 높이는 문제로 끝나지 않는다.
동일한 물리 FOV·렌즈·센서·장면을 유지하고 Scene 간격만 줄여 타일별
광학·평균 광전자 결과가 수렴하는지 확인해야 한다. 각 타일에는 저장
PSF와 왜곡 역변환이 요구하는 원본 Scene halo를 포함하고, 실행 메모리는
작업 프로세스 단위로 관측·제어해야 한다. 수치 수렴은 저장된 기하 PSF
모델 안에서의 판정이며 회절, 광량, CRA×픽셀 각도 응답 및 제품 MTF의
물리 검증을 대신하지 않는다.

### 같은 물리 시야의 타일 해상도 수렴 진단

`run_streamed_offaxis_mean_sampling_convergence()`는 분석용 연속 slanted
edge, 수평·수직 물리 FOV, 렌즈 상태, 분광 광량, 전역 센서 좌표·CFA·노출을
고정한다. Scene의 행·열만 두 배씩 늘리고 각 격자에서 기준 edge와
같은 각도로 이동한 edge를 **둘 다 사전 검사한 뒤** 주변부 센서 타일의
잡음 전 평균 광전자를 계산한다. 두 연속 해상도 증가에서 평균 광전자와
edge 이동 응답이 선언 허용치 안에 들 때만 선택한 수치 모델의
`numeric_offaxis_mean_sampling_converged_model_only`를 반환한다. 느슨한
허용치가 오인되지 않도록 상대 허용치 입력은 각각 최대 10%로 제한한다.

작은 합성 strict PSF·8×8 센서 fixture에서 32²→1024² 입력까지 시험했고,
선언한 5% 기준을 마지막 두 증가에서 만족했다. 256²→512² 때 최대
평균 광전자 변화는 1.90%, 고정 edge 이동 응답 변화는 1.65%였고,
512²→1024² 때는 각각 1.05%, 0.89%였다. 이것은 수렴 판정 로직의
실행 검증이다. 저장 P0014 렌즈나 실제 DEF active mode의 수렴을 실행한
결과가 아니며 RAW, ISP, 제품 MTF·FOV 적격을 뜻하지 않는다.

[P0014 동일 FOV 주변부 격자 사전 검사](../reports/camera_platform/mtf-support-20260928/p0014-fixed-fov-offaxis-grid-preflight-dev40.json)는
위 첫 저장 주변부 field 근처의 같은 8×8 센서 타일을 고정하고 Scene
격자만 두 배씩 늘린 **실행 전 정책 검사**다. 센서는 2048×14000,
0.575606513 µm로 둔 가상 default 센서이며 저장 DEF 제품의 active
mode가 아니다.

| Scene 격자 | OI 간격 | PSF 격자 | 사전 검사 결과 |
| --- | ---: | ---: | --- |
| 2048×14000 | 0.5756 µm | 547² | 선언 정책 안에서 실행 준비 |
| 4096×28000 | 0.2878 µm | 1091² | source tile 질의 491만 + halo 질의 368만이 800만 건 정책 초과 |
| 8192×56000 | 0.1439 µm | 2179² | halo 질의만 1468만 건으로 정책 초과 |

뒤의 두 거부는 **하드웨어 메모리 부족 판정이 아니다**. 1배의 준비 상태도
해당 센서 타일의 계산·수렴·제품 FOV를 뜻하지 않는다. 이전에 주변부
36² 광학 ROI를 실행한 기록과 이번 사전 검사는 범위가 다르다.
또한 halo 질의 수 정책 초과를 Scene 경계 충돌로 잘못 표시하던
주변부 사전 검사 상태를 분리했다.

## dev41: 정책 한도와 실제 자원 소모의 분리

기본 800만 기하 질의는 하드웨어 한계가 아니라 보수적인 **총 작업량
정책**이다. 이제 호출자가 더 큰 총 질의 예산을 명시할 수 있고, 광학
halo의 역왜곡 경계 검사는 한 번에 최대 65,536셀씩 분할한다. 전체
좌표 배열을 메모리에 올리는 방식으로 상한만 높인 것은 아니다. 작은
격자에서 분할·일괄 검사의 보수적 원본 경계가 일치했고, 저장 P0014의
4배 고정 FOV 격자에서는 세 파장에 걸친 1,468만 질의를 231개 청크로
검사해 기존 일괄 경계와 동일한 결과를 얻었다. 이는 사전 검사이며
광학 convolution·RAW 실행·실제 peak RSS 확인은 아니다.

[같은 P0014 자산·FOV의 갱신된 사전 검사](../reports/camera_platform/mtf-support-20260928/p0014-fixed-fov-offaxis-grid-preflight-dev41.json)에서
총 질의 예산을 명시적으로 3,200만 건으로 올리면 1배·2배·4배 격자
모두 `ready_under_declared_policy`다. 4배의 타일 질의 1,592만 건과 halo
질의 1,468만 건을 합하면 **3,060만 건**이며, 정책 작업 메모리 추정은
약 **5.49 GiB**다. 이 값은 실제 최대 RSS가 아니다. 총 예산을 1,600만
건으로 선언하면 같은 4배 입력은 다시 차단된다. 따라서 이전 2배·4배
거부를 물리 RAM 한계로 읽는 것은 잘못이다. 이번에는 세 격자의
**사전 검사만** 수행했고 수치 수렴이나 고밀도 RAW는 실행하지 않았다.

`AnalyticCaptureResourcePolicy`의 최대 추정 작업 예산 8 GiB도 현재
코드의 **진단 admission 정책**이다. 이 호스트의 물리 RAM은 2026-09-28
`os.sysconf`로 32 GiB가 관측됐지만, RAM 전부가 한 계산에 가용한 것은
아니다. 거부된 정책값만으로 하드웨어 불가능을 판정할 수 없고, 정책에
통과한 계산도 실제 메모리 peak나 실행 완료를 보장하지 않는다.

타일 광학 경로는 전역 Scene·OI 배열을 피한다. 그러나 기존 CameraE2E
`resample_sensor_capture()`는 전체 센서의 광전자 평균을 받은 뒤 잡음,
고정 패턴, gain, ADC와 결과 배열을 전역 크기로 생성한다. 새 진단
`compute_streamed_full_raw()`는 센서 타일의 평균 광전자를 전역 좌표와
CFA 위상을 보존해 합친 후 **한 번** readout한다. 작은 합성 fixture에서
원래 전체 CameraE2E 계산과 정수 RAW가 일치한다. 이 연결은 광학 메모리
문제를 줄이지만, 전역 readout 배열의 메모리 문제를 없애지 않는다.

현재 readout admission의 256 bytes/센서셀은 안전 측으로 둔 추정 계수이지
측정 peak 또는 하드 제한이 아니다. 예를 들어 2048×14000 가상 센서는
이 계수만으로 약 6.84 GiB이고, 5천만 센서셀은 약 11.92 GiB다. 모델
배열·최대 광학 타일·Python/NumPy 임시 배열은 별도로 고려해야 한다.
수천만 화소 RAW의 제품 실행을 주장하려면 별도 프로세스에서 실제
RSS·시간을 계측하고 강제 자원 제한 또는 out-of-core readout을 갖춘 뒤
판정해야 한다. 저장 DEF 센서의 제품 active mode가 확인되지 않았으므로
현재 큰 가상 센서 격자를 제품 FOV로 해석하지 않는다.

## dev42: 주변부 실제 계산과 저장 자산 기반 진단 연결

### 저장 P0014 주변부의 실제 1배·2배 계산

[동일 FOV의 관측 프로세스 실행 기록](../reports/camera_platform/mtf-support-20260928/p0014-fixed-fov-offaxis-mean-observation.json)은
저장 P0014 기하 PSF, 동일 2048×14000 가상 센서의 주변부 8×8 타일,
동일 분광 광량·노출·두 edge 위치를 고정하고 Scene 격자만
2048×14000에서 4096×28000으로 늘렸다. 두 격자의 광학 convolution과
센서 **잡음 전 평균 광전자**를 실제로 계산했다. 자식 실행 시간은 각각
17.34초와 96.02초였고 직접 자식 RSS의 폴링 최댓값은 441 MB와
694 MB였다. 폴링은 순간 최대 RSS를 보장하지 않고 하드 메모리 제한도
아니다.

1배에서 2배로 바꾸자 두 edge 위치의 평균 광전자 최대 변화가 공통
신호 스케일의 **10.96%**, edge 이동 응답 변화가 **3.48%**였다. 이는 1배
격자를 안정된 해상도로 판정할 수 없다는 *수치 모델 내부* 근거다.
2배가 수렴했다는 뜻도 아니다. 수렴 판정에는 다음 4배와의 비교 및
두 번 연속의 허용 오차 통과가 필요하다. 4배는 이 Mac에서 하드 RSS
제한이 검증되지 않아 실행하지 않았다. 이 관측은 정수 RAW·ISP·물리
MTF·제품 FOV의 검증이 아니다.

### active mode 선언 자산의 입력 경계

내부 `preflight_fullfield_study()`는 로컬 절대 경로의 선언 파일을 받는
오프라인 검사다. 이를 HTTP 요청에 그대로 노출할 수 없으므로
`register_active_mode_declaration()`과
`preflight_registered_fullfield_study()`를 추가했다. 선언의 JSON 값을
저장 센서 revision·CFA에 묶어 Registry의 content-addressed blob과
불변 record에 보관하고, 후속 검사는 record ID로만 호출한다. 반환
결과에서도 내부 파일 경로를 제거한다. typed artifact owner index에
선언 원본을 연결해 API artifact URL로 원본을 다시 받을 수 있다.
`POST /api/v1/lens-sensor-active-modes` 등록과
`POST /api/v1/lens-sensor-fov/registered-inspect` 검사는 SDK에서도
지원한다. 현재 화면에는 아직 연결되지 않았다. 선언 자체도 엔지니어가
제공한 미검증 자료이므로 저장 광학 모델 지원 범위의 계획만 허용한다.

### 저장 DB 쌍의 타일 RAW 진단 작업

`lens_sensor_streamed_raw` 작업은 Lens DB revision과 저장 Sensor DB run을
고정한 뒤, 분석용 D65 slanted-edge Scene의 필요한 광학 타일을 strict
기하 PSF로 계산한다. 센서 타일별 평균 광전자를 하나의 작은 RAW 격자로
합치고 CameraE2E digital readout을 **한 번** 적용한다. API·Worker·SDK에서
제출, 공통 작업 대기, 원본 RAW·평균 광전자 artifact 조회가 연결됐다.
템플릿에는 기존 렌즈·센서 catalog 검색 결과가 포함되고, SDK의
`lens_sensor_streamed_raw_sensor_option(identifier, run_id)`는 선택한
저장 센서의 pitch와 native CFA를 반환한다. 사용자는 이 pitch를 가상
crop에 선언해야 하며, 저장 32×32 응답 crop을 제품 active mode로
자동 승격하지 않는다.

이 경로의 Scene 입력 상한은 축별 512, 가상 센서 crop은 축별 16픽셀이다.
이는 **진단 API 정책**이며 고해상도 전체 RAW를 실행했다는 의미가 아니다.
256² Scene→8² RAW 통합 시험이 통과했고, 기본 512² Scene→8² RAW 직접
계산은 약 52초에 완료됐다. 그 조건의 관측 `ru_maxrss`는 약 404 MiB였다.
두 실행은 수치 수렴 또는 제품 active mode를 검증하지 않는다. 현재 job은
결과 배열을 통째로 바이트로 직렬화·재검증하므로 상한만 올려서는 대형
RAW에 적합하지 않다. 디스크 기반 타일 광학·행 단위 readout과 스트리밍
artifact 저장 계약을 별도로 완성해야 한다.

## dev43: 저장 DB의 디스크 기반 RAW 실행과 메모리 정책

별도 [`lens_sensor_out_of_core_raw` 작업](camera-platform-out-of-core-raw.md)은
저장 Lens DB revision과 Sensor DB run을 고정해 strict 기하 PSF 광학 타일을
계산하고, 평균 광전자·정수 RAW·기준 DN·전압을 디스크 메모리맵과 Registry
artifact로 보존한다. 모든 센서 타일의 기하·halo 사전 검사를 완료하고
순서·해시를 확인한 계획을 재사용한다. 전체 높이의 readout 묶음은 전체
RAW 배열을 메모리에 만들 수 있으므로 거부한다. Worker가 종료시킨 자식의
임시 출력은 작업 ID에 묶어 회수한다. Worker 자체의 강제 종료 후 남은
파일은 자동 회수하지 않는다.

[설치된 dev43 wheel의 실제 DB 실행 기록](../reports/camera_platform/mtf-support-20260928/disk-raw-32x32-proof.json)은
32×32 **가상 중심 crop**, 1024×1024 Scene, 1.0° Scene FOV, 광학 센서
타일 4개에서 별도 Worker 자식이 약 87.95초에 RAW를 저장했음을 확인한다.
설치본의 `camera_platform`·`pyisetcam` import와 Registry의 여섯 artifact,
저장 파일 SHA-256 재검증까지 확인했다. 광학 convolution 간격은 약
0.422 µm, 센서 pitch는 1.22 µm로 입력 표본은 센서 픽셀당 약 2.89개다.
이는 **수치 RAW 실행**의 증거이며 Scene 격자 수렴, 제품 FOV, 물리 MTF,
실측 정확도나 peak RSS의 증거가 아니다.

새 작업의 256×256 센서·4096×4096 Scene, 광학 타일 추정 512 MiB·결합
추정 1 GiB·출력 32 MiB, Worker 기본 600초는 운영자가 선언한 입장 정책이다.
이 Mac에서 조회한 물리 RAM 32 GiB 또는 하드웨어가 정한 최대 해상도가
아니다. dev43 작업 자체로는 256×256 실행과 4배 P0014 고정 FOV 수렴을
확인하지 않았다.
입장 사전 검사를 통과해도 실제 최대 RSS나 완료 시간이 보장되지 않는다.

## 2026-09-28 추가 관측: 저장 P0014 동일 FOV의 2배·4배 주변부

[격리 실행 기록](../reports/camera_platform/mtf-support-20260928/p0014-fixed-fov-offaxis-mean-2x-4x-observation.json)과
[재현 스크립트](../tools/verify_camera_p0014_offaxis_mean_4x.py)는 수평 FOV
13.068°와 저장 P0014 광학 상태, 가상 센서의 같은 주변부 8×8 타일을
고정하고 2배·4배 Scene 격자에서 기준 edge와 이동 edge의 **네 조건**을
각각 별도 자식 프로세스로 계산했다. 기존 2배 결과와 재실행한 평균 광전자
해시·소스 식별자·광학 상태 해시가 일치한다. 2배→4배의 잡음 전 평균 광전자
최대 변화는 공통 신호 스케일의 **0.72546%**, edge 이동 응답 변화는
**0.75929%**였다. 앞선 1배→2배 평균 변화가 10.962%여서 두 연속 단계의
허용 기준 통과가 없으므로 **수치 수렴 판정은 아직 `false`**다.

4배 두 조건은 각각 약 8분에 정상 종료했고, 직접 자식 RSS의 폴링 최대는
약 1 GiB였다. 폴링은 순간 peak를 포착하는 하드 제한이 아니다. 사전 검사의
약 5.49 GiB 작업 메모리 추정은 보수적인 **입장 정책값**이지 측정 RSS나
하드웨어 상한이 아니다. 다음 8배 격자에서는 예상 PSF 축이 현재 4097표본
상한을 넘으므로 추가 수렴 단계도 현재 계약에서는 실행할 수 없다. 이번 결과는
**가상 주변부 8×8 평균 광전자**에 한정된다. 정수 RAW, 전체 FOV, 제품
active mode, 회절·광량·CRA×픽셀 응답을 포함한 물리 MTF는 검증하지 않았다.
