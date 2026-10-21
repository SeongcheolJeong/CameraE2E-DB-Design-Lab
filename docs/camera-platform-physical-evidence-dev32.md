# Camera Platform dev32: 물리 결합과 평가 근거 우선

CameraE2E의 저장 기하 raytrace PSF는 영상 계산에 이미 사용된다. 이번 작업은
PSF 적용 여부를 다시 구현하는 대신, 동일 Lens 처방의 광량·입사 방향·회절
PSF를 실제 DB revision에서 재현 가능한 Factory 작업으로 만들고, Sensor 각도
응답·MTF·HDR 판단에 필요한 증거를 분리한다. 아래 모든 새 광학 계산은 연구용
`PROPOSED` 결과다. 실물 정확도나 제품 순위 승인으로 해석하지 않는다.

## Lens DB revision → 광량/방향 + 회절 PSF

`lens_db_factory_workflow`는 검토된 Lens DB revision ID와 원본 CSV SHA-256을
요구한다. 응답 파장·시야·픽셀 pitch·두 해석기의 수치 샘플링을 지정하면,
원본 처방과 연구 가정을 다시 검사하고 물리 처방 지문이 같은 두 Factory 요청을
계획한다. 두 작업은 일반 Factory Worker에서 실행되며, 원본 CSV와 catalog
revision snapshot은 draft Lens asset에 연결된 다운로드 가능 artifact로 남는다.
해시가 바뀐 계획은 제출되지 않는다.

API 경로는 `GET /api/v1/lens-db-factory-pairs/template`,
`POST /api/v1/lens-db-factory-pairs/plan`,
`POST /api/v1/lens-db-factory-pairs`다. Python SDK의
`lens_db_factory_pair_template()`, `plan_lens_db_factory_pair()`,
`submit_lens_db_factory_pair()`가 같은 경로를 사용한다. 두 번째 작업 제출이
실패하면 첫 작업 ID가 오류에 명시되며 같은 계획을 재시도하면 재사용한다.
두 작업을 하나의 원자적 트랜잭션으로 묶은 것은 아니다.

완료된 두 작업은 `POST /api/v1/lens-db-factory-pairs/models` 또는 SDK의
`publish_lens_db_factory_pair()`로 명시적으로 draft 등록한다. 등록 전에
동일 처방·원본 CSV·asset·실행 환경·수치 판정·f-number 조건과 두 후보 모델
계약을 재검사한다. 부모 광량/방향 모델과 자식 회절 PSF 모델은 서로의
lineage를 보존한다. 두 revision이 같은 CSV 바이트를 공유할 때에도 각
asset의 정확한 artifact 소유권을 검증한다.

[실제 p0014 Worker 실행](../reports/camera_platform/integrated-workflows-20260928/actual-p0014-factory-pair-dev32.json)에서
550/650 nm 광량·입사 방향 Job은 `PROPOSED` 후보와 artifact를 저장했다.
동일 계획의 회절 PSF Job은 `Prescription wavefront exceeded its compute deadline`
으로 실패했다. 앞서 직접 해석한 [dev29 회절 PSF](../reports/camera_platform/integrated-workflows-20260928/dev29-p0014-restricted-band/verification.json)는
약 99.46초에 `PROPOSED`였지만 별도 저장 연구 결과이므로 실패한 Factory
Job을 성공으로 바꾸거나 자동 결합하지 않는다. 이번 두 Job의 경과는 약
121.71초였으며, 원인으로 처리 부하나 실행 경로 차이를 아직 구분하지 못했다.
따라서 이번 실행에서는 결합 Lens 모델과 Lens×Sensor RAW가 생성되지 않았다.

회절 계산의 수치 합격 기준은 유지하고 내부 계산 예산을 180초, 해당 Factory
Worker의 기본 실행 예산을 210초로 유한하게 늘린 뒤 **동일 요청**을 새
Registry에서 재시험했다. [재시험 증거](../reports/camera_platform/integrated-workflows-20260928/actual-p0014-factory-pair-dev32-retry.json)의
광량·방향과 회절 PSF Job은 모두 `succeeded`/`PROPOSED`이며 전체 경과는
약 119.35초다. 이것은 해석 작업의 성공이지 실물 Lens 정확도 또는 Sensor
결합 성공이 아니다.

`0.16.2.dev32` 계산 wheel을 격리 설치해 재실행한
[설치본 증거](../reports/camera_platform/integrated-workflows-20260928/installed-p0014-factory-pair-dev32.json)는
wheel SHA-256과 설치 파일 1047개를 대조하고, 별도 Worker에서 실제 p0014
두 작업을 다시 수행했다. 광량/방향과 PSF가 모두 `PROPOSED`였으며,
SDK/API 등록과 동일 요청 재시도에서 두 draft 모델 ID가 유지됐다. 실행
환경을 바꾼 과거 Job을 새 패키지의 성공으로 재사용하지 않았다.
격리 Registry와 89개 content-addressed blob은
[보존 상태](../reports/camera_platform/integrated-workflows-20260928/installed-p0014-state-preservation-dev32.json)에
기록했고, 설치본으로 다시 열어 부모·자식 모델 lineage와 artifact 해시를
재검증했다.
두 모델의 `physical_validation=not_established`, `product_ranking_allowed=false`다.
[최종 wheel 설치 확인](../reports/camera_platform/integrated-workflows-20260928/final-wheel-install-dev32.json)은
README 안내를 반영해 다시 빌드한 wheel의 설치 파일을 대조했다. 두 wheel의
[바이트 비교](../reports/camera_platform/integrated-workflows-20260928/wheel-runtime-equivalence-dev32.json)에서
달라진 항목은 패키지 `METADATA`와 `RECORD`뿐이고 1046개 나머지 파일의
내용은 동일하다. 따라서 위 실제 계산 결과는 최종 wheel의 동일 실행 코드에
대한 검증으로 연결된다. 최종 wheel은
[보존 Registry의 모델 링크도 다시 읽었다](../reports/camera_platform/integrated-workflows-20260928/final-wheel-preserved-state-readback-dev32.json).
최종 wheel 자체의 별도 광학 재계산을 주장하지 않는다.

실제 p0014 처방의 effective diameter는 검증된 물리 clear aperture가 아니라
명시적 연구 proxy다. 재료 분산·stop power·scalar diffraction 가정도
남는다. 따라서 이 결과만으로 Lens×Sensor 광전자율이나 RAW를 승인하지 않는다.

## Sensor 각도 응답의 정확한 부족 항목

[자산 감사](../reports/camera_platform/integrated-workflows-20260928/native-response-availability-20260928.json)는
현지 QE 모델 224개와 FDTD/LUT 자산을 검사했다. QE catalog에는 독립
R/Gr/Gb/B site별 파장·극각·방위 응답 격자가 없고, 센서별 FDTD 응답 3건은
4030개 목표 표본 중 완료 0개·미검증이다. 기존 1.4 µm LUT는 p0014의
3.0 µm PSF와 정규화가 맞지 않는다. 합성 3.0 µm fixture는 650 nm가 없다.
그러므로 현재 저장 자산으로 p0014의 550/650 nm 방향 광선과 native 픽셀
응답을 결합한 물리 광전자율/RAW 경로는 거부한다.

`native_response_assets.inspect_native_response_bundle()`은 새 측정/해석
응답이 들어올 때 원본·응답·qualification 파일 해시, 독립 site basis,
collection 중복 여부, PSF pitch/파장 및 실제 양의 power 광선의 각도 범위를
확인한다. 현재 이는 intake 검사이며 센서 모델 등록·Lens 결합 자동 실행은
아니다. 이 관문을 통과해도 **연구 계산 적합성**만 뜻한다. 원본 작성자와
수치값의 진위, 실물 카메라 정확도는 별도 검증이 필요하다.

## MTF·HDR·인지 해석 경계

[MTF 입력 지지 감사](camera-platform-mtf-support-qualification.md)는 저장된
2×2 품질 Job의 공통 물체각 점수화 ROI에서 후단 재표본화 경계 민감도를
검사했다. 네 시스템의 경계 여유는 5/6/9/9 픽셀, 필터 반경은 3/3/1/0
픽셀이며 두 경계 조건의 RGB 차이는 0이다. **카메라마다 픽셀 ROI 면적이
다르다는 사실 자체는 비교 금지 사유가 아니다.** 아직 광학 PSF·픽셀 적분의
입력 지지, 장면 halo, 독립 수렴 근거가 없어 물리 MTF와 제품 순위는 차단된다.
새 품질 Job은 실제 ISP 구현 ID와 profile halo를 저장·재조회하며, 과거 Job의
누락 ID를 소급해 채우지 않는다.

[HDR/LFM 모드 준비도](camera-platform-hdr-lfm-mode-readiness.md)는 보존된
RAW·fusion Job과 출처가 선언된 timing mode를 다시 검사하고, 부족한
operating point·native crop·phase·실행 프로그램·계측 근거를 코드로 돌려준다.
원본 측정 RAW/독립 기준율/모드·파형·기준 로그를 넣는 Worker 비교 경로도
있다. 현재 보존된 통합 시험은 **합성 reference fixture**이므로 Worker 성공이
물리 HDR/LFM 성공을 뜻하지 않는다. 제품 판단은 `inconclusive`다.
[dev32 설치본 HDR/LFM 체인](../reports/camera_platform/integrated-workflows-20260928/dev32-installed-hdr-lfm/hdr-lfm-mode-readiness-bounded.json)은
출처를 선언한 timing fixture → PWM RAW → HDR 재구성 → 기준율 비교까지
Worker로 통과했지만, 독립적으로 확인된 제품 mode와 실제 기준자료는 없다.
[설치본 API 확인](../reports/camera_platform/integrated-workflows-20260928/installed-api-surface-dev32.json)은
HDR/LFM 준비도·기준자료 제출 경로와 native 응답 검사 모듈의 설치·노출을
확인한 것이며, 해당 실제 측정자료를 제출하거나 제품 HDR/LFM을 검증한 결과는
아니다.

기존 작은 합성 mAP는 라벨과 예측을 연결한 진단이다. 독립 검증된 실제 물체
정답과 모델 추론이 없어 인지 성능 판단에 사용할 수 없다. 고급 ISP와
최적화도 현재 조건별 연구 경로로 남아 있으며, 위 광학·센서·시간·인지
근거가 갖춰지기 전에 범용 제품 최적화로 부를 수 없다.

## 다음 결합 기준

출처와 정규화가 확인된 3.0 µm R/Gr/Gb/B 각도 응답을 550/650 nm와
실제 Lens의 유효 광선 각도 범위에서 확보한다. 이미 등록한 같은 처방의
광량/방향·회절 PSF draft 모델에 해당 응답을 결합할 때 `included_effects`가
QE·CFA·microlens·collection을 중복 적용하지 않는지 확인한다. 이어서
동일 물체각 장면의 광학·픽셀 입력 지지와 독립 샘플링 수렴을 저장하고,
독립 RAW/기준율과 실제 인지 라벨로 검증한다. 이 조건이 충족되기 전에는
상용제품 순위·HDR/LFM 합격·물리 MTF·인지 mAP 주장을 차단한다.
