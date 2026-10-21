# E-SFR 112 Edge + 색·계조 IQ 운영 계약

문서 기준: 2026-10-19, `calibration-esfr-iq-20261018/runtime-csv` 구현.
화면은 `/esfr-measured.html`이며 DB Design Lab의 정확한 `run_id / candidate_id / condition_id`에 연결한다.
이 문서는 구현 계약을 설명한다. 실물 촬영 검증 완료나 8780 배포 완료를 의미하지 않는다.
현재 실제 카메라 촬영 근거는 없으며 합성 소프트웨어 검증과 구분해야 한다.

## 1. 촬영 → 보정 → 새 검증

1. 같은 고정 조건에서 개발 RAW·ISP **3쌍**을 등록한다. RAW 형식, native 좌표, CFA, black/white level, 카메라·조건·프레임 ID를 장비 근거로 채운다. 미확인 값을 임의 기본값으로 채우지 않는다.
2. 4개 기준점으로 제안한 ROI를 원본에서 검토한다. 112개 edge 전체와 IQ용 18개 색·20개 계조 패치 전체가 필요하다. 개발 촬영별 검토를 기록한다. 제안 좌표 자체는 정렬 승인이 아니다.
3. 개발 3쌍만 사용해 위치별 유효 MTF와 IQ 후보를 피팅한다. MTF 목적함수는 native RAW Gr SFR이다. IQ는 중간 계조의 WB·노출 스케일, 색 패치의 제한된 3×3 CCM, 계조 전방 응답 모델을 추정한다.
4. MTF·IQ 후보, 코드·ROI·명목 기준 hash와 취득 계약을 고정한다. 화면이 `awaiting_validation`에 도달한 뒤 검증 촬영을 취득한다.
5. 별도 session에서 동일 조건으로 **새 검증 3쌍**을 취득한다. 같은 후보 계약을 연결하고 원본·픽셀 hash와 시간 순서를 확인한다. 개발 리뷰의 원래 operator와 행을 유지한 채 새 3쌍 리뷰를 추가한다.
6. 검증에서는 재피팅하지 않는다. 고정 후보 재실행, 112개 위치의 RAW·ISP SFR, 색·계조·노이즈 비교와 통합 보고서를 생성한다. 실패와 미제공 값도 보존한다.

새 UI 작업에서는 IQ가 기본 선택된다. 사용자가 해제하면 기존 112개 edge 경로만 실행한다. IQ 정보가 없는 과거 workflow는 소급 변경하지 않는다. 이 staged 경로의 표본 계약은 **3+3**이며, 초기 일반 Lab 문서의 5+5 권장과 혼동하지 않는다. 표본 수는 시스템 프로토콜이며 ISO 요구 수가 아니다.

촬영 설정·장비·시각·ROI 검토는 operator 선언이다. 서버의 hash·계보·시간 순서 확인은 외부 카메라의 실제 정체나 노출 시각 인증을 대체하지 않는다. 입력 지원 범위와 준비 절차는 [촬영 가이드](../src/camera_platform/static/esfr-measured-guide.html)를 따른다.

## 2. 반복 노이즈: 기본 3개 + 선택 0–5개

`noise_repeat_ids`는 기본 3개와 겹치지 않는 **추가 촬영 ID**다. 각 단계 총 3–8프레임이며 8프레임을 권장한다. 추가 자료도 동일 역할·고정 조건·38개 패치 검토가 필요하다.

- 추가 촬영은 패치 반복 노이즈 전용이다. MTF core-three 피팅·재실행이나 CCM 피팅에 추가하지 않는다.
- 개발에서 추가 수를 고정한다. 검증도 같은 추가 수를 후보 고정 후 새로 취득한다.
- 검증의 추가 촬영은 core3와 같은 세션에 속할 수 있지만 정확한 후보·계약·원본 snapshot에 묶인 admission을 통과해야 한다. 일반적인 세션 중복 예외가 아니다.
- RAW R/Gr/Gb/B와 ISP RGB의 공간 통계와 시간 분산·SNR을 분리한다. 공간 통계에는 차트 질감·불균일이, 시간 변동에는 조명 드리프트가 포함된다. Dark/PTC·전자 gain·read noise·full well·전체 DR을 추정하지 않는다.

## 3. 색 기준과 출력의 의미

명목 기준 ID는 `imatest_esfr_2023_nominal_v1`이다. 제조사 설계 **Lab D50** 값에서 XYZ D50을 얻고, 명시된 **Bradford D50→D65** 행렬을 거쳐 linear sRGB D65 타깃을 만든다. 기준 행렬·관측자·출처·hash를 보존한다. 실물 인쇄물의 분광 반사율이나 촬영 광원 SPD를 측정한 값은 아니다.

RAW는 `(native DN − site black) / (site white − site black)`으로 한 번 정규화한다. 촬영별 자동 재스케일이나 추정 DN/e−를 섞지 않는다. 정량 행렬 출력의 음수·1 초과 값을 자르지 않으며 미리보기만 표시 범위로 제한한다.

| 비교 단계 | 의미 |
|---|---|
| Reference ISP 보정 전 | 같은 RAW의 고정 black/white 정규화와 native bilinear, identity CCM |
| WB·노출 스케일만 | 개발에서 고정한 WB·노출 스케일, identity CCM |
| Reference ISP 보정 후 | 같은 RAW에 고정 WB·노출 스케일·CCM 적용 |
| 실제 ISP | 같은 노출의 카메라 출력 관측; 변경하지 않음 |

전→후는 WB·노출·CCM의 종합 효과이고 WB-only→후가 같은 정규화에서 CCM의 효과다. 실제 ISP의 sRGB **색 공간** 확인은 sRGB **encoding/transfer** 선언과 별개다. 색 공간 미확인, 미적용 ICC 또는 회색 영상은 명목 ΔE와 RGB 색 오차를 미제공으로 남기며 관측값·통계는 유지한다.

계조 모델은 관측 범위의 `명목 반사율 → RAW Green / 실제 ISP 휘도` 전방 응답이다. 영상에 추가 적용하는 tone correction이 아니며 외삽하지 않는다. IQ 허용치는 미설정이므로 결과는 `diagnostic_no_acceptance_thresholds`와 판정 대기다.

**IQ CCM 및 MTF+IQ 복합 후보는 자동 활성화하지 않는다.** `db_active_model_updated: false`인 조건부 연결 후보로 저장한다. 기존 MTF 경로의 통과 시 조건부 프로파일 활성화 여부와는 별개이며 `profile_activated`만으로 CCM 적용을 선언하면 안 된다.

## 4. 통합 재실행과 보고서

`combined_replay`는 저장된 112개 native 유효 MTF 재실행 배열에 고정 WB·노출·CCM을 적용한다. 독립 3프레임 × 112개 = 336개 관측을 비교하며 CCM 전후 RAW hash는 같다. DB 렌즈 처방의 PSF를 새로 계산한 결과, 순수 렌즈 PSF 식별 또는 제조사 ISP 재현이 아니다.

기존 SFR 기준은 RAW 0.03 / ISP 0.05로 유지한다. 제조사 ISP가 diagnostic인 경우 ISP는 진단이고 RAW 판정만 사용한다. 이 기준은 현재 engineering workflow의 곡선 RMSE 기준이며 카메라 출하 기준이나 ISO 적합성 기준이 아니다. IQ 판정 기준을 새로 만들지 않는다.

HTML에는 전체 차트의 RAW·Reference ISP 전후·실제 ISP 이미지, WB-only 단계의 색 오차, 18색·20계조 전체 패치, 제외 사유, 공간·시간 통계, 112개 통합 재실행 오차와 기존 RAW gate를 남긴다. Reference 미리보기는 native 256px tile + 2px halo 처리 뒤 표시용으로 축소한다. 정량 값은 축소 영상에서 다시 측정하지 않는다.

주요 산출물은 `report.html`, `iq-result.json`, `composite-candidate.json`, `frozen-iq.json`, `acquisition-contract.json`, `preview-manifest.json`, `combined-replay/`, `bundle-manifest.json`, `bundle.zip`이다. ZIP manifest는 포함 파일의 SHA·크기를 검증한다. IQ ZIP에 원본 RAW/ISP 전체가 포함된다고 가정하지 말고 capture owner/hash와 원본 조회 경로를 이용한다.

새 보고서는 `color-patches.csv`, `gray-tone.csv`, `noise-patches.csv`, `provenance.csv`도 자동 생성한다. 개발·검증 역할, 패치·촬영 ID, 원본·프로파일·기준 해시, 제외 사유와 계산 불가 상태를 보존한다. 미제공 수치는 빈칸으로 남기고 상태·사유 열로 구분한다. 분산은 제곱 단위로 표기한다. 추가 반복의 관측 해시는 core3의 색 피팅 자료와 별도로 기록한다.

완료된 수치 결과의 출력 형식만 보완할 때는 별도의 불변 export revision을 발행한다. 기존 보고서·프로파일·입력·수치 결과를 덮어쓰거나 다시 피팅하지 않는다. 원 계산의 source fingerprint와 보고서 출력 코드의 fingerprint를 따로 기록하며, DB와 작업 화면은 새 출력의 링크를 제공한다. 출력 개정은 새 독립 검증이나 카메라 성능 개선을 의미하지 않는다.

## 5. API와 스키마

아래 경로는 `/api/v1/camera-validation/esfr` 기준이다. 변경 요청에는 기존 로컬 세션의 `X-Platform-Token`을 사용한다.

| 메서드·경로 | 계약 |
|---|---|
| `POST /captures` | multipart `metadata`, `raw`, `isp` 원본 등록 |
| `GET /measured/template` | 빈 실측 metadata·검증 추가 필드·IQ 입력 계약 |
| `POST /measured/review-contract` | 원본 SHA·ROI digest에 결합한 명시적 촬영별 검토 |
| `POST /measured/workflows` | `source`, `development_ids[3]`, `rois`, `chart_profile`, `measured_review`, `roi_reviewed_capture_ids`, 선택 `iq` |
| `GET /measured/workflows/{id}` | status/stage/current_job, contract, iq_frozen/iq_result, links, source_kind; 운영 상태 자체는 독립 수치 증거가 아님 |
| `POST /measured/workflows/{id}/validate` | `validation_ids[3]`, 새 리뷰+변경하지 않은 개발 리뷰, `iq_patch_roi_reviewed_capture_ids`, `iq_noise_repeat_ids` |
| `POST /measured/workflows/{id}/resume` | 저장된 영수증으로 중단 단계 재개; 원본·후보·코드 검증 유지 |
| `GET /measured/workflows/{id}/iq/files/{relative}` | 해당 불변 보고서 manifest에 있는 파일만 조회 |
| `POST /measured/workflows/{id}/iq/exports` | 빈 객체 `{}`로 완료 수치 결과의 불변 출력 개정 발행; 동일 코드·원본이면 같은 개정 반환 |
| `GET /measured/workflows/{id}/iq/exports/{export_id}/files/{relative}` | 해당 출력 개정에 포함된 HTML·CSV·JSON·ZIP 조회 |
| `GET /measured/iq-latest?source_run=…&candidate=…&condition=…` | 정확한 DB tuple의 최신 IQ 보고서 또는 `report: null` |
| `POST /measured/iq-comparisons` | `{workflow_a_id, workflow_b_id}` 완료 보고서 비교 |
| `GET /measured/iq-comparisons/{id}/files/{relative}` | 모드 비교 보고서·근거 |

생성 요청의 `iq`는 `enabled`, `reference_id`, `actual_isp_color_space: srgb|unknown`, `patch_roi_reviewed_capture_ids`, `noise_repeat_ids`를 받는다. 패치 리뷰 ID는 core3와 모든 추가 반복을 포함한다. 조건·ROI·기준·반복 수는 후보 고정 뒤 바꾸지 않는다.

| 스키마 | 용도 |
|---|---|
| `camera_esfr_measured_workflow_v1` | 불변 입력·DB 조건·코드 결합 |
| `camera_esfr_iq_v1` / `camera_esfr_iq_profile_v1` | 관측·평가 / WB·노출·CCM·계조 후보 |
| `camera_esfr_iq_frozen_v1` | 개발 전용 후보 고정 영수증 |
| `camera_esfr_iq_validation_admission_v1` | 정확한 새 검증과 patch-only 추가 반복의 계약 |
| `camera_esfr_condition_bound_composite_v1` | 유효 MTF + reference ISP 조건부 복합 후보 |
| `camera_esfr_iq_report_v1` / `camera_esfr_iq_bundle_v1` | 통합 보고서 / ZIP 포함 파일 manifest |
| `camera_esfr_iq_export_v1` | 원 수치 결과를 보존한 불변 보고서 출력 개정 |
| `camera_esfr_iq_combined_replay_v1` | 112개 유효 MTF + 고정 IQ 재실행 |
| `camera_esfr_iq_comparison_v1` | 두 완료 workflow의 차트 범위 비교 |

## 6. 합성 소프트웨어 시험과 HDR 비교

`POST /measured/software-workflows`는 합성 core3로 동일 staged 경로를 검증하는 별도 진입점이다. 생성 후 GET/validate/report 경로는 공통이며 `source_kind: synthetic`을 끝까지 유지한다. 실측 리뷰를 가장하거나 source_kind를 measured로 바꾸지 않는다. `tools/verify_esfr_iq_live.py`는 개발 자료 생성·후보 고정 뒤 새 holdout 생성·ZIP 검증을 수행한다. 생성기의 비공개 정답은 피터 입력으로 주지 않는다. 이 시험의 성공은 물리 검증이 아니며 `physical_validation: not_established`가 유지된다.

일반/HDR 비교는 각 모드에서 보정과 새 검증을 완료한 **서로 다른 두 workflow**를 비교한다. source_kind·명목 기준·차트 개정·native 패치 ROI·카메라/geometry와 광학·조명을 유지한다. 조건 차이는 mode/exposure/gain에 한정한다. 한 조건 안에서 반복의 조건 일치를 완화하지 않는다. 두 모드는 각각 고정된 CCM을 가지므로 차이를 센서 하드웨어만의 효과로 해석하지 않는다. HDR 합성·움직임·flare·노출 sweep이나 전체 동적 범위 검증은 포함하지 않는다.

## 7. 소스 고정과 재현

실행 중 `src/`, 수치 코드와 연결된 실행 환경을 수정하지 않는다. workflow의 `source_code`, 현재 fingerprint, 서버가 적재한 환경이 다르면 새 작업 또는 전환이 차단된다. 소스가 바뀐 기존 workflow의 기록을 지우거나 hash를 갱신해서 계속하지 않는다. 새 release를 별도로 준비하고 계산 종료 후 기존 workspace/state-dir을 보존해 배포한다.

이 문서 추가는 실행 소스 변경이나 서버 재시작을 수행하지 않는다. 개발·검증 원본, 이전 실패, 후보·계약·보고서와 SHA를 보존하고 실제 배포/검증 상태는 해당 실행 영수증으로 확인한다.
