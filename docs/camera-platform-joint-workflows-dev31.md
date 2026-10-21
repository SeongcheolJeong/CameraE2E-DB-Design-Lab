# Camera Platform 0.16.2.dev31: 광학 결합 계약과 설치본 시간 RAW

이 개발판은 [dev30 광학·MTF 기록](camera-platform-joint-workflows-dev30.md)의
수치 근거를 유지하고, 서로 다른 수치 샘플링으로 계산한 동일 Lens 처방의
회절 PSF와 광량·방향 응답을 결합할 수 있는 계약을 추가한다. 등록 R1 합성
모델의 시간 RAW도 외부 기본 자산에 의존하지 않도록 수정했다. 두 변경은
계산·재현 계약의 진전이며 실물 Lens×Sensor 정확도나 제품 승인 근거는 아니다.

## 동일 Lens 처방, 독립 수치 샘플링

`lens_prescription_v3`–`v5`의 정규화된 요청에서 **`sampling` 한 필드만**
제외한 버전 지정 물리 처방 지문을 계산한다. Factory는 지문과 PSF/광량
각각의 전체 요청 해시를 함께 저장하며, 저장된 카메라 실행을 재조회할 때도
동일한 규칙과 원본 characterization 해시를 검사한다. 파장·field·면·재질·
stop·초점·물체 조건 등 물리 입력이 바뀌면 거부한다. v1/v2와 동일 샘플링
요청에는 기존 전체 요청 일치 규칙을 유지한다.

dev29의 p0014 **550/650 nm** 회절 PSF 요청과 dev30 방향·상대조도 요청은
물리 처방 지문이 일치하고 수치 샘플링만 다르다. 이 둘은 모두 저장된
`PROPOSED` 연구 결과이며, 현재 승인된 결합 Lens 모델·광전자율·RAW가
만들어진 것은 아니다. Factory→등록 Lens→RAW·재처리와 archived replay,
binding 위조 거부를 포함한 관련 회귀 **121개**가 통과했다.

[dev31 wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev31-py3-none-any.whl)의
SHA-256은 `2fab9f0a7a7d18dfb5a50c2331efc4c48d75bf307e4b256651a10a6a7c02a05c`이다.
격리 설치본에서 패키지 파일 1,038개의 바이트 일치를 확인했다.

## 센서 픽셀 각도 응답 관문

[자산 감사](../reports/camera_platform/integrated-workflows-20260928/dev30-sensor-angular-availability.json)는
p0014의 550/650 nm 방향 응답과 조건이 맞는 출처 확인 native 픽셀 응답을
찾지 못했다. 조사한 FDTD 생산 파일은 0/4030 표본·미검증이고 2.9 µm이며,
저장 PSF 격자는 3.0 µm다. 기존 1.4 µm split-PD LUT는 수치 파일이 빠진
메타데이터이고, 3.0 µm native 예제는 500/600 nm의 합성 입력이다.
`physical_estimate_v1`의 명목 응답에는 마이크로렌즈 각도 routing 손실이
적용되지 않고 수집효율은 가정값이다. 따라서 현재 자산으로 CRA×픽셀
광전자율과 해당 제품 RAW를 승인하지 않는다. 감사 JSON에 필요한 파장·각도·
방위·격자·기준면·수집·출처 계약을 기계 판독 가능하게 기록했다.

## HDR/LFM, MTF, 인지·ISP 상태

dev30 설치본은 정적 R1 HDR가 성공했으나 동적 RAW에서 외부 upstream 자산
다운로드를 시도해 실패했다. [실패 기록](../reports/camera_platform/integrated-workflows-20260928/r1-timed-dev30-installed-failure.json)을
보존했다. dev31은 병합 여부와 무관하게 등록 R1 전기 모델의
`hdr_bracket_v1` 시간 적분·RAW 취득을 사용한다. 병합을 끈 동적 RAW에도
외부 자산 접근 금지 회귀를 넣었다. 이 경로는 16×16·500/600 nm 합성 입력의
원본 RAW/정적 HDR 또는 PWM 진단에 한정된다. 움직임 보정, vendor LFM과
Lens DB×Sensor DB HDR의 제품 성능 결론은 없다.
[dev31 설치본 증거](../reports/camera_platform/integrated-workflows-20260928/r1-timed-dev31-installed-proof.json)는
SDK template→submit→wait, 별도 Worker, 정적 HDR와 동적 RAW의 원본 artifact
재조회 및 해시 일치, 움직이는 장면·부분주기 PWM의 병합 제출 거부를 담는다.
검증은 기존 가상환경 의존성을 이용한 격리 wheel 설치와 FastAPI TestClient로
진행했으며 기본 `127.0.0.1:8770` 서버를 변경하지 않았다.

2×2 Lens×Sensor 화질 Job은 공통 물체각 점 중심 ROI와 실제 점수화 범위를
기록한다. 그러나 전체 광학·픽셀·ISP 입력 지지와 경계 민감도가 검증되지
않아 물리 MTF 비교와 제품 순위는 계속 차단한다.
[비교성 검사](../reports/camera_platform/mtf-comparability-20260928/verification.json)는
기존 지표 숫자를 바꾸지 않는다. 서로 다른 픽셀 면적 자체는 비교 금지
사유가 아니다.

기존 합성 fixture mAP는 제공된 상자와 예측의 연결 진단이다. 독립적으로
검증한 실제 물체 정답과 모델 추론·대표 장면이 없어 인지 성능 근거가 아니다.
고급 ISP·최적화도 선언된 제한 후보 공간의 연구 도구이며 광학/센서/시간/인지
지표를 모두 유효성 검증한 범용 제품 탐색으로 승격하지 않았다.

다음 결합 실험은 출처·정규화·수집효율이 확인된 550/650 nm native 픽셀
각도 응답을 확보하거나 적격 조건으로 새로 계산한 뒤, 동일 처방 PSF와
방향 응답을 승인 가능한 모델로 묶어 `photoelectron_rate`와 실제 정수 RAW를
보존하는 것이다. 그 다음 동일 물체 좌표의 MTF 입력 지지, 동적 HDR 정답,
독립 인지 라벨과 모델 추론을 검증해야 한다.
