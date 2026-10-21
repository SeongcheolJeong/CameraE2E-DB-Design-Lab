# 기존 CameraE2E 계산 엔진의 플랫폼 채택 범위

이 문서는 `src/pyisetcam`의 **기능 존재**와 `src/camera_platform`의 **현재 연결**을 구분한다. 기준은 이 저장소의 개발 소스다. 동결된 `0.16.1rc2` 배포물이나 기본 실행 서버의 제공 범위를 뜻하지 않으며, 아래의 모든 기존 기능을 이번에 재실행했다는 주장도 아니다. 새 연결 작업의 입력·한계는 [통합 작업 계약](camera-platform-integrated-workflows.md)에 별도로 적었다.

| 상태 | 뜻 |
| --- | --- |
| `engine_available` | 기존 `pyisetcam`에 계산 API가 있다. 필요한 객체·자산·조건을 직접 구성해야 한다. |
| `platform_connected` | 명시된 플랫폼 API·SDK·Worker 작업이 입력 검사, 실행, 출처와 결과 보존까지 연결한다. 표에 적힌 좁은 계약에만 해당한다. |
| `conditional` | 자산, 모델 적용 범위, 선택 패키지, 보정 또는 추가 adapter가 있어야 한다. |
| `unsupported` | 현재 명시된 플랫폼 작업으로 해당 조합이나 제품 수준 주장을 할 수 없다. |

| 영역 | `engine_available` | 현재 `platform_connected` | `conditional` 또는 `unsupported` 경계 |
| --- | --- | --- | --- |
| Scene→광학→센서→ISP | 객체별 구성, 단계별 계산, 정적·시퀀스 시나리오 실행이 가능하다. | 플랫폼 자체 R1 모델·장면 계약과 아래의 제한된 DB 작업이 있다. | 임의의 기존 Scene, 렌즈 revision, 센서 run, ISP 설정을 하나의 자유로운 카메라 조합으로 자동 변환하는 일반 adapter는 없다. |
| Lens DB·광학 | 처방 검색, 저장 raytrace PSF 로딩, 별도 파동광학·광학 계산 API가 있다. | [`/api/v1/db-comparisons`](camera-platform-db-integration.md)은 두 Lens revision의 기하 spot·CRA를 비교한다. 별도 [`lens-sensor-raw`](camera-platform-integrated-workflows.md)는 Lens DB v2 기하 PSF 한 개와 저장 `db_estimate_v1` 센서 한 개로 정수 RAW 미리보기를 계산한다. | 결합 RAW는 렌즈 절대 투과율·RI를 1로 가정한 중앙 시야 수치 preview다. 현행 DB PSF의 광량·회절·센서 픽셀 샘플링은 검증되지 않았다. 제품 선예도·감도 순위가 아니다. |
| 저장 Sensor DB | 저장 run의 native CFA·분광 응답·readout을 새 Scene에 적용할 수 있다. | DB 비교는 두 pinned run의 동일한 선언 flat 입력 RAW를 저장한다. [`saved-sensor-quality`](camera-platform-saved-sensor-quality.md)는 1–2개 pinned run의 flat·edge·Macbeth 및 단일 노출 HDR 차트 진단을, [`saved-sensor-search`](camera-platform-saved-sensor-search.md)는 한 pinned run의 제한된 노출·gain grid를 제공한다. | DB run마다 실행 코드·자산 hash, readout, CFA, 각도 조건을 확인한다. 두 센서의 같은 raster 크기는 같은 물리 화각이 아니다. 지표와 탐색 결과는 제품 순위나 실측 정확도가 아니다. |
| FDTD·TCAD | 기존 LUT 적용과 별도의 센서 물리 추정 경로가 있다. | [`/api/v1/physics-lut`](camera-platform-physics-lut-bridge.md)은 **기본 generic 센서**에서 FDTD field 광흡수 비율 또는 TCAD 수집 비율 하나의 적용 전후 RAW를 비교한다. | 저장 Sensor DB run에 LUT를 임의로 곱하면 물리 효과를 중복 계산할 수 있다. 이 작업은 공간 crosstalk·실측 QE·제품 수집효율의 검증이 아니다. |
| 화질·인지 | MTF·색·시각·검출·분할 평가기가 있다. | [`catalog-quality-comparisons`](camera-platform-quality-comparison.md)은 catalog로 **새로 생성한** 센서 추정 모델을, `saved-sensor-quality`는 **저장된 run**을 각각 제한된 장면·지표로 평가한다. | 두 출처를 같은 run의 결과로 섞지 않는다. Macbeth 보정 잔차는 독립 색 정확도가 아니다. 일반 task-perception 작업은 현재 이 DB 선택 흐름에 연결되지 않았으며 모델 package·weight·정답 라벨도 필요하다. |
| 탐색·최적화 | 기존 엔진에 grid, random, Latin-hypercube, evolutionary, surrogate, GP와 사용자 목적함수가 있다. | `saved-sensor-search`는 고정 이상 광학·균일 장면에서 최대 네 노출·gain 후보를 평균 광전자 수와 포화 제약으로 평가한다. | 기존 엔진의 기본 목적함수는 RGB 평균 최대화다. MTF·ΔE·mAP를 저장 run의 목적함수로 자동 제공하지 않는다. 정밀 FDTD·TCAD·RayOptics 확대 **계획**은 solver 실행 증거가 아니다. |
| 시간·HDR·LFM | 기존 엔진은 선형 단일 노출의 행별 시간 적분과 별도 legacy multi-exposure 함수를 갖는다. 플랫폼 자체에도 범위가 정해진 시간·HDR·LFM 연구 경로가 있다. | `saved-sensor-quality`의 HDR 차트는 단일 노출 진단이다. 별도 `saved-sensor-hdr`는 두 저장 센서 RAW를 정적 브래킷으로 합성한다. `saved-sensor-lfm`은 같은 저장 센서의 위상별 PWM 시간 적분 RAW를 비교한다. | HDR은 FPN을 명시적으로 제거한 민감도 실험이며, LFM의 line time은 선언 조건이다. 임의의 staggered HDR·움직임 보정·제품 LFM 성능·TNR 계약은 아니다. |
| ISP | 기존 demosaic·색 보정·display 렌더링 API가 있고, 플랫폼은 별도 RAW 재처리 경로를 갖는다. | 위 화질 작업은 지원되는 CFA에서 제한된 선형 ISP 결과를 계산한다. `saved-sensor-isp-replays`는 그 작업에 저장된 동일 native RGGB RAW에 ISP v2의 고정 WB·CCM·tone·sharpen을 적용한다. | LSC·denoise는 같은 취득 도메인의 보정 자산이 없어 거부한다. 모든 CFA·HDR stream·비선형 readout의 범용 ISP가 아니다. `pyisetcam.ip_compute()`의 `wgt_blur`·`network_demosaic` 인수는 현재 구현에서 사용되지 않는다. |
| RAW 데이터셋 | 기존 exporter가 NPZ·메타데이터·라벨·선택적 ZIP 및 시퀀스를 만든다. | `retained-raw-datasets`는 저장 화질 작업의 첫 원본 RAW를 값·dtype·seed를 유지한 NPZ/ZIP으로 옮기고 기존 validator와 RAW-aware perception indexer를 실행한다. | 한 프레임의 라벨 없는 데이터셋이다. RGB/시퀀스 전체 export, task score, DNG, 라벨 자동 추적은 아직 연결되지 않았다. |

플랫폼 기능을 추가할 때에는 기존 API를 먼저 재사용하되, 선택 자산의 단위·기준면·CFA·파장·시야·노출·readout·정규화와 원본 hash가 새 작업의 입력 계약에 들어가는지 확인한다. 조건이 충족되지 않으면 원인을 남기고 그 조합을 실행하지 않는다. 계산 가능성, 모델 적용 범위, 교차 도구 일치, 실측 정확도는 각각 별도 증거로 기록한다.

근거: [`pyisetcam` 기능 설명](camerae2e-technical-overview.md), [`sensor_capture.py`](../src/pyisetcam/sensor_capture.py), [`temporal.py`](../src/pyisetcam/temporal.py), [`ip.py`](../src/pyisetcam/ip.py), [`optimization.py`](../src/pyisetcam/optimization.py), [`dataset.py`](../src/pyisetcam/dataset.py), [`task_perception.py`](../src/pyisetcam/task_perception.py), [`camera_platform/api.py`](../src/camera_platform/api.py).
