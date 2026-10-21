# Extended E-SFR ISO 2023 Camera Validation Lab

첫 버전: 2026-10-18. 화면 `/camera-validation-esfr.html`, API `/api/v1/camera-validation/esfr`.
독립 파일 분석 화면으로 동작하며 DB Design Lab과 기존 Calibration Lab에서 접근한다.

## 촬영 준비

- 차트 제품·버전·크기·재질·개체 ID를 기록한다. 현재 보유 차트의 개별 MTF/색 기준값은 미확보다.
- 카메라와 차트를 평행하게 고정하고 균일 조명, 반사/포화 방지, 고정 초점·노출·gain·WB·ISP 조건을 사용한다.
- 같은 촬영의 Bayer RAW/BIN과 ISP PNG/JPEG/TIFF를 짝지어 보존한다. RAW packing, stride/header, bit depth/shift, CFA, black/white level과 crop 원점을 입력한다.
- 권장 개발 5쌍 + 다른 session의 검증 5쌍. 검증 촬영은 개발 피팅에 사용하지 않는다. 조명/초점/위치가 달라지면 새 조건의 시험으로 나눈다.
- ISP crop/resize가 다르면 `unregistered`를 선택한다. 현재 버전은 명시적으로 대응된 동일 native raster만 위치별 RAW/ISP 오차를 함께 평가한다.

## 분석과 보정

1. 원본을 등록한다. 파일 hash, 디코딩 픽셀 hash, 카메라/세션/취득 계보를 고정한다.
2. edge ROI 제안을 검토한다. 자동 제안은 직선 edge 후보이며 보유 차트의 완전 자동 인식으로 인증하지 않는다. 회색·색·wedge ROI는 원본 좌표에서 추가·수정한다.
3. 사전에 곡선 RMSE 기준을 정한다. `raw_sfr_rmse`, `isp_sfr_rmse`는 정규화된 SFR 전체 곡선 오차이며 %, MTF50 오차가 아니다. 데모 기준 0.03/0.05는 데모용이며 실제 카메라 출하 기준이 아니다.
4. RAW R/Gr/Gb/B와 ISP RGB/Y를 원본 좌표에서 측정한다. 4배 ESF binning, windowed LSF와 derivative correction을 사용한 자체 engineering estimator다. ISO/Imatest 동등성 검증은 미확립이다.
5. 개발 자료에서 위치별 유효 Gaussian PSF와 edge geometry/명암을 피팅하고 고정한다. 방향이 충분히 독립적이면 타원 covariance를 추정하고, 두 직교 방향만 있으면 등방성 제약을 사용한다.
6. 별도 검증 자료에서 측정·보정 전·후 RAW/ISP와 곡선 오차를 비교한다. ROI별 실패/미측정은 숨기지 않는다. 기준 없는 보고서는 inconclusive다.
7. 명시적 기준에 pass한 후보를 E-SFR 전용 활성 프로파일로 선택 적용한다. 적용 후 재실행은 고정 후보를 재피팅하지 않으며 같은 저장 촬영에 대한 동작 확인임을 표시한다.

## 측정 항목과 경계

| 항목 | 출력 | 식별하지 않는 것 |
|---|---|---|
| slanted edge | ESF/LSF/SFR, MTF50/50P, overshoot/undershoot, 위치별 지도 | 순수 렌즈 PSF, 비대칭 PSF 위상 |
| RAW 채널 edge | 채널별 SFR, 상대 색 edge 위치 | 렌즈만의 색수차 확정 |
| 회색/색 패치 | 채널 평균, 공간 편차, 반복 촬영 시간 분산/SNR | QE, PTC, dark noise, 기준 없는 ΔE/CCM/절대 OECF |
| wedge | 진단 ROI 통계 | 주파수 좌표가 없는 절대 해상도 판정 |

현재 보정 전 영상은 수동 Gaussian 초기값, 보정 후 ISP는 고정 bilinear demosaic 기준이다.
실제 DB 렌즈 PSF나 하드웨어 ISP 설정을 바꿨다는 뜻이 아니다. DB 연결은 선택한 설계 조건과 측정 후보의 계보다.
차트 응답 미확보 상태에서 카메라와 차트 blur를 분리하지 않는다. MTF magnitude만으로 PSF가 유일하게 결정되지 않는다.

## 보고서와 원본

HTML(그림 내장), result/candidate/request JSON, ROI 오류 CSV, 이미지/배열, SHA manifest, ZIP을 보존한다.
HTML에는 native 위치 지도, 전체 차트 preview, 검증 ROI의 입력/보정 전/후 RAW·ISP, 곡선과 품질 사유가 포함된다.
원본 총량 256MiB 이하는 ZIP에 포함한다. 더 큰 입력은 `original-captures.json`의 hash와 개별 원본 다운로드 URL로 유지한다.
원본 파일당 128MiB, paired capture 40MP, study 64 captures/256 ROIs, native ROI 면적 합 8MP, 단일 edge 262144 pixels 제한이다.

## 기존 Calibration Lab 연결

`export-calibration`은 동일 ROI/역할의 반복 SFR을 평균하고 편차와 모든 원본 계보를 보존한다.
필수 센서 pitch/촬영 조건이 없으면 임의 값으로 채우지 않는다. legacy 지원 범위(RGGB/짝수 raster/ROI 수/SFR 범위)에 맞지 않으면 blocker를 반환한다.
이 경로의 curve 기반 PSF 초기화는 본 Lab의 같은-pixel-operator 피팅과 구별한다.
`db-baseline` API는 조건이 맞는 기존 DB sampled PSF를 읽어오지만 현재 UI 비교 renderer의 수동 Gaussian baseline을 자동 대체하지 않는다.

## 운영

다른 센서 계산의 root 소스를 보존하기 위해 기존 adaptive release를 기반으로 분리된 runtime에 구현했다.
`outputs/camera-validation-esfr-20261018/current-server/serve_8780.py --check`로 release hash를 확인한다.
동일 launcher는 8780이 비어 있을 때만 실행하며 기존 workspace/state-dir을 그대로 사용한다. 기존 실행·모델을 삭제하지 않는다.

공식 참고: https://www.imatest.com/imaging/iso-12233/ , https://docs.imatest.com/docs/esfriso_instructions , https://docs.imatest.com/docs/mtf-compensation
