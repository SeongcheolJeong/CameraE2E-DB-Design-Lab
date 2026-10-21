# Camera Platform dev18: CameraE2E PSF와 결합 RAW의 후속 평가

`0.16.2.dev18`의 Lens DB × Sensor DB RAW 경로는 기존 CameraE2E 광학 계산을 사용한다. `lens_sensor_raw_bridge.py`가 해시로 고정한 렌즈 revision의 저장 raytrace PSF를 `lens_patent_raytrace_optics()`로 로드하고, `camerae2e_run_scenario()` → `camera_compute()` → `oi_compute()`에서 파장·시야 위치별 PSF convolution을 수행한 뒤 저장 센서 모델의 `digital_raw`로 계산한다. bounded 모드는 같은 strict geometric PSF 연산을 필요한 kernel만 streaming하여 실행한다. 별도의 단순 단일-kernel blur로 대체하지 않는다.

[dev18 wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev18-py3-none-any.whl)의 SHA-256은 `b468fc917386703108b2b03a1e73686a9333c04ab5ae1198b43a33dd07dc41a3`이다. 별도 설치 경로의 1,031개 파일을 wheel과 바이트 비교하고, 아래 세 사례를 각각 격리된 Registry와 child Worker에서 실행했다. 이 wheel은 일반 설치 위치나 기존 8770 서버를 교체했다는 증거가 아니다.

| 설치본 사례 | 확인된 결과 | 증거 |
| --- | --- | --- |
| 두 Lens DB revision × 두 Sensor DB run | 동일 256×256 D65 slanted-edge 입력을 기존 PSF convolution과 센서 계산에 넣어 네 조합의 32×32 `int32` 원본 RAW를 보존했다. 공통 물체각 겹침 안에서 각 native 중앙 ROI의 MTF50 선별 진단값은 L1S1 52.94, L1S2 50.91, L2S1 75.45, L2S2 76.25 cycles/degree였다. 각 ROI는 32×32, 28×28, 20×20, 16×16으로 각도 면적이 정확히 같지는 않다. | [2×2 RAW·화질 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev18-installed-proof.json) |
| 출처가 고정된 행 시간 → PWM/LFM → HDR | 센서 revision/readout/가상 창/노출 쌍에 결속한 JSON 모드를 SHA-256으로 가져오고 재다운로드했다. 선언한 20µs와 40µs 조건의 2노출×4위상×8×8 정수 RAW 중 235/512 값이 달라졌다. 40µs LFM 원본 RAW는 HDR 저장 결과에도 바이트 동일하게 남았고, 맞지 않는 모드는 제출 시 422로 거부됐다. | [타이밍·HDR 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev18-installed-timed-proof.json) |
| 두 장면의 RAW → ISP → 제공 예측 → 인지 코호트 | `uniform`과 `slanted_edge` 각각의 저장 strict-PSF RAW와 ISP 영상에서 별도 이미지 ID를 보존했다. 주석·예측·모델 manifest를 해시로 결속한 두 단일 프레임 평가를 묶어 영속 Job과 export까지 실행했다. 이미지별 매칭 mAP 진단값은 0.667이고 `product_map.available=false`다. | [인지 코호트 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev18-perception-cohort-installed-proof.json) |

화질 비교는 같은 수평 화각, 충분히 촘촘한 입력 장면, 중앙부 공유 물체각 범위, 작은 색별 중심 이동, 공간 표본 간격의 국소 균일성, 비포화 영상과 Nyquist 한계를 확인한 경우에만 숫자를 낸다. 서로 다른 센서의 native cycles/pixel 값을 직접 빼는 비교는 계속 막고, 물체각 cycles/degree의 짝비교만 **모델 내 선별 진단**으로 허용한다. 각 native ROI와 ISO 내부 crop의 각도 면적은 서로 달라 엄밀한 동일 면적 MTF 비교가 아니다. 렌즈 투과·상대조도는 unity 가정이고 PSF는 저장 geometric raytrace 모델이다. 회절, CRA와 실제 픽셀 각도 응답의 결합, PSF sampling, 물리 카메라 정확도는 검증되지 않았다. 따라서 `product_ranking_allowed=false`다.

타이밍 모드 import의 다운로드 URL이 이전에는 404가 되는 결함도 고쳤다. 가져온 JSON과 선택적 출처 파일을 변경 불가능한 `timing_modes` 기록의 증거로 등록하고, 조회 시 파일 SHA와 소유 기록을 다시 확인한다. 출처 파일을 고정해도 그 안의 line-time 수치를 자동 추출하거나 검증하지는 않는다. 현재 저장 센서 run에는 확인된 HDR/LFM 제품 모드가 없고 가상 창의 실제 die 위치와 절대 PWM 위상도 모른다. 이번 HDR 결과는 `inconclusive`이며 제품 HDR/LFM 성능을 뜻하지 않는다.

인지 코호트의 박스는 고정된 **연결 검사용 fixture**다. 검출기를 실행하지 않았고 합성 패턴에 의미상 실제 물체 정답도 없다. 현재 주석 출처 계약이 `human_annotation`과 `independent_reference`만 허용하므로, 이 fixture는 예측 전에 별도로 작성했다는 제한된 뜻으로 후자를 사용했다. 출처 URI와 `unknown` 불확실성에 이 한계를 적고 제품 mAP를 비활성화했다. 하지만 이 명칭은 오해의 여지가 있어 `synthetic_contract_fixture` 같은 별도 유형과 대표 장면·독립 라벨·실제 모델 추론 계약이 다음 단계에 필요하다.

현재 이 코호트는 `uniform_d65`와 `slanted_edge_d65` 두 장면 계열을 각각 한 번만 허용한다. 이전의 2–64건 요청 안내는 실행 가능한 범위와 맞지 않아 정확히 2건으로 고쳤다. 독립 장면 자산 ID가 추가되기 전에는 더 큰 코호트를 지원한다고 표시하지 않는다.

관련 소스·단위/API 테스트 54개와 artifact-owner 회귀 포함 53개가 통과했다. 최종 계약 문구·코호트 입력 범위 변경 뒤 직접 대상 15개를 다시 통과시켰고, 새 코드 Ruff와 workbench JavaScript 계약도 통과했다. 이는 설치본 실행·저장·비교 계약의 증거다. 범용 고급 ISP 전체, 대표 해상도 인지 성능, 대규모 데이터셋, 제품 HDR/LFM과 물리 검증까지 완료했다는 뜻은 아니다. 기존 dev17의 재사용 ISP 보정과 라벨 RAW cohort는 동일 wheel에 포함되지만, 이 문서의 세 설치본 사례는 그 경로를 모두 다시 실행한 결과가 아니다.
