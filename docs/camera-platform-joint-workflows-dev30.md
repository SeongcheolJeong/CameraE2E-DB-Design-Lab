# Camera Platform 0.16.2.dev30: 광학·시간·MTF 경계

이 개발판은 기존 CameraE2E의 저장 기하 PSF를 RAW에 적용하는 단계에서 멈추지
않고, 같은 Lens DB 처방의 회절·방향별 광량 계산과 등록 R1 자산의 시간 촬영을
별도로 진전시킨다. 아래 `PROPOSED`는 **선언된 수치 모델의 수렴 관문** 통과를
뜻한다. 실물 렌즈·센서 정확도나 상용 제품 순위를 승인하지 않는다. 기본
`127.0.0.1:8770` 서버의 설치 상태는 이 소스 버전과 별개다.

## Lens DB 원본과 광학 응답

저장 기하 raytrace PSF가 `preview_lens_sensor_raw()`의 RAW 경로에 적용되는
사실은 유지된다. 이 경로의 렌즈 상대 조도·절대 투과율은 아직 미확인 unity
가정이며, 센서 픽셀의 방향 응답과 같은 광선별로 결합되지 않았다.

저장 기하 PSF가 준비된 Lens revision 4개 중 3개에서 현재 자산 묶음의 검토된
CSV가 빠져 있었다. 별도 Lens Patent DB 원본에서 각 CSV를 가져와 SHA-256과
SQLite 처방 fingerprint가 정확히 일치하는 경우만 복원했다. p0014의 별도
외부 CSV는 현재 검토본과 다르므로 덮어쓰지 않았다. [복원 증거](../reports/camera_platform/integrated-workflows-20260928/dev30-verified-lens-csv-restoration.json)에
경로·해시·revision을 보존했다.

광각 렌즈의 S26/S27에는 A12 항이 실제로 있다. A12를 무시하면 clear aperture
가장자리 sag가 각각 약 10.1/12.0 µm 달라지므로 명시적 v3 비구면 계약을
추가했다. 기존 v1/v2 입력은 그대로 유지된다. 36개 원본 면을 보존한 광각
처방이 OPD/PSF **연구 요청 가능** 상태로 바뀌었으며, 광각 회절 PSF의 수치
수렴이나 물리 정확도까지 확인한 결과는 아니다. [A12 검증](../reports/camera_platform/integrated-workflows-20260928/dev30-a12-review.md).

기존 p0014와 p019 처방은 이미지면 바로 앞의 25 mm 반경 평판 때문에 전체
후면 원판을 샘플링하면 각도 상한 88.87°로 거절됐다. 새로운 평행판 공기
구간 경계는 **선언된 개구와 굴절률** 아래에서 통과 가능한 광선의 각도를
상한으로 묶고, 센서 필드 점을 중심으로 한 완전한 원판을 각 고리의 실제
면적으로 적분한다. 뒤쪽 고리를 생략하지 않는다. 독립적인 공기/유리 평판
검사에서 65,536개 광선 통과 판정이 일치했고 축상 적분 상대 오차는
0.0161%/0.0259%였다. [독립 검토](../reports/camera_platform/integrated-workflows-20260928/dev30-planar-tail-independent-closure.json).

p0014의 **550/650 nm** 방향·상대조도 계산은 기존 100초보다 짧은 약 16.93초
동안 coarse/fine 두 단계에서 `PROPOSED`가 됐다. 최대 변화는 상대조도
0.007621(허용 0.03), 각도 CDF 0.014043(허용 0.05), irradiance 적분
0.005141(상대 허용 0.03)이다. [원본 결과·배열](../reports/camera_platform/integrated-workflows-20260928/dev30-planar-tail-directional-final/result.json)을
보존했다. 이번 450/550/650 nm 3파장 설정은 `REFUSED`다. 앞서 통과한
[550/650 nm 회절 PSF](../reports/camera_platform/integrated-workflows-20260928/dev29-p0014-convergence-review.md)와
광학 면·파장·field·stop은 같지만 **수치 샘플링 제어값이 달라** 현재 Factory의
정확한 parent-request 일치 관문을 통과하지 못한다. 결합된 Lens 방향·PSF
모델이나 Lens×Sensor 광전자율/RAW로 승격한 것은 아니다. 실제 투과율,
재질 분산, clear aperture, CRA×픽셀 응답도 검증되지 않았다.

## HDR/LFM 및 MTF

등록 R1 합성 모델·장면을 시간 knot와 적용 노출·gain에 묶어
`r1_timed_acquisition` Job/API/Worker로 실행한다. 원본 int32 RAW와 프레임별
RGB를 분리 저장하고 재조회 시 해시·shape·dtype을 검사한다. 신고된 정적
장면에서만 HDR 병합을 허용하며 움직이는 knot나 부분주기 PWM에는 원본 RAW
진단만 허용한다. [입력·사용법](camera-platform-r1-timed-acquisition.md). 이것은
16×16·500/600 nm 등록 합성 예제 파일럿이며 기존 Lens DB×Sensor DB 카메라의
범용 HDR/LFM, 움직임 보정 또는 vendor 성능 검증이 아니다.

설치본 검증에서 dev30의 정적 HDR은 성공했으나 동적 RAW Worker는
`TemporalSceneRuntime`이 외부 upstream 자산 다운로드를 시도해 실패했다.
[실패 증거](../reports/camera_platform/integrated-workflows-20260928/r1-timed-dev30-installed-failure.json)를
보존했다. 위 동적 RAW 설명은 dev30 wheel의 성공 증거가 아니며 수정판 재검증이
필요하다.

기존 2×2 Lens×Sensor 화질 결과의 공통 550 nm 점 중심 각도 ROI는 있으나,
broadband 광학·픽셀 적분·ISP가 실제로 사용한 입력 지지 영역과 경계 처리가
같은 물체 좌표에 충분히 포함됐다는 근거는 없다. 제품 MTF 비교 허용을 계속
`false`로 두고 점수화 ROI와 빠진 증거를 기계 판정으로 기록한다. 센서마다
픽셀 면적이 다른 **것 자체**는 MTF 비교 금지 이유가 아니다. 비교 대상의
차이를 유지한 채 공통 물체 영역 및 각 시스템의 입력 지지를 입증해야 한다.
[MTF 검사](../reports/camera_platform/mtf-comparability-20260928/verification.json).

다음 통합 관문은 두 광학 수치 작업의 샘플링 차이만 허용하는 **버전 지정
물리 처방 fingerprint**를 만들어 동일 파장·면·개구·stop·field를 다시 검사하고,
출처가 확인된 센서 픽셀의 550/650 nm 방향 응답을 연결해 ADC 이전
`photoelectron_rate`를 실제로 계산·보존하는 것이다. 이어 동일 물체 좌표에서
MTF 입력 지지를 검증하고, 동적 HDR의 정답·시간·노출 조건 및 ISP·인지
평가를 확장해야 한다. 실물 측정과 제품 수준 검증은 별도 관문이다.
