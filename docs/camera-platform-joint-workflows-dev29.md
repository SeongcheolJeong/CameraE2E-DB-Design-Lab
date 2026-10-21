# Camera Platform dev29: PSF 수치 범위와 화질 비교 문구 정합

현재 소스 개발판은 `0.16.2.dev29`이다. [dev28의 2×2 화질·LFM 증거](camera-platform-joint-workflows-dev28.md)에 이어, 저장 Lens 처방의 회절 계산 가능 범위를 실제 실행으로 확인하고 화질 Job의 모순된 비교 문구를 고쳤다. 이 문서는 격리 개발판의 근거이며 기본 `127.0.0.1:8770` 서버가 교체됐다는 기록은 아니다.

## Lens DB 처방의 회절 PSF: 550/650 nm 연구 결과만 통과

[p0014 수치 수렴 보고서](../reports/camera_platform/integrated-workflows-20260928/dev29-p0014-convergence-review.md)는 원본 특허 표와 해시로 묶인 28개 면, 고정 센서면, 네 모서리 field의 `550/650 nm` 계산을 기록한다. 기존 RayOptics/Prysm 경로의 54²/108² pupil 및 5/11 subpixel refinement에서 커널 L1 변화 `0.01935 ≤ 0.03`, 최소 보존 에너지 `0.96227 ≥ 0.95`, 최대 인접 OPD `0.24770 ≤ 0.25` wave였다. 저장 광학 배열을 생산 계산과 독립적인 직접 Fourier 합으로 다시 확인한 32개 구성의 최대 bin-energy 차이는 `4.44e-16`이다. [저장 배열과 전체 요청](../reports/camera_platform/integrated-workflows-20260928/dev29-p0014-restricted-band/verification.json), [독립 감사](../reports/camera_platform/integrated-workflows-20260928/dev29-p0014-array-audit.json), [수렴 그림](../reports/camera_platform/integrated-workflows-20260928/dev29-p0014-convergence.png)을 보존했다.

이는 **미등록 `PROPOSED` 연구 PSF**다. 계산 시간이 `99.462 s`로 현행 `100 s` 한계에 가깝고 OPD 여유도 작다. 원래 `450–650 nm` 전 파장 요청은 계속 `REFUSED`이다. 450 nm는 현재 분산 가정·고정 센서면에서 심한 수치 defocus를 보여 128² 단일-field 진단에서도 인접 OPD가 `0.7071` wave였다. 해당 진단은 전체-field 수렴 검사가 아니다. 파장을 빼거나 초점을 바꾸어 전체 RGB가 통과했다고 기록하지 않는다. 특허의 effective diameter를 실제 clear aperture로 쓰는 것, nd/Vd 기반 유리 분산, 절대 투과율도 물리적으로 검증되지 않았다.

## 2×2 화질 Job: 각도 격자 진단과 native RAW 비교를 구분

기존 공통 각도 격자 MTF는 네 ISP 영상을 같은 물체각 ROI로 재표본화한다. 그런데 Job 상위의 실패 이유가 이 경로 자체도 없다고 설명해 결과가 모순됐다. 이제 상위 실패 이유와 pairwise 이유·scope는 **native cycles/output-pixel 비교가 차단된 이유**만 말한다. 공통 각도 MTF와 [550 nm 기하 셀 면적 proxy](camera-platform-common-grid-footprint.md)는 별도 모델 진단으로 남는다. 과거 저장 Job의 정확한 이전 설명 문구와 신규 proxy 필드 부재만 읽기 시 정규화하며, 보존된 숫자·소스·배열은 다시 계산해 대조한다. 화질 테스트 16개에 이전 Job 판독과 숫자 변조 거부를 포함했다.

[격리 설치본 화질 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev29-quality-installed-proof.json)는 새 Registry와 child Worker에서 2개 Lens × 2개 Sensor의 저장 RAW·ISP·화질 Job을 끝까지 실행한 결과다. 같은 16×16 물체각 격자에서도 네 기하 셀 면적의 최대/최소 비율은 `3.393`이다. 이 수치는 실제 PSF·픽셀 적분 footprint가 아니다. `pixel_integrated_effective_area_matched=false`, `product_ranking_allowed=false`를 유지한다.

## 제품 판단을 막는 남은 조건

| 항목 | 현재 확정된 범위 | 다음 통과 조건 |
| --- | --- | --- |
| 렌즈×센서 RAW | 기존 저장 기하 PSF를 적용한 제한된 중앙 장면 계산 | 전체 사용 파장·field에서 회절 수렴, 절대 렌즈 광량, CRA×픽셀 각도·분광 응답을 같은 출처 계약으로 결합 |
| MTF | 동일 물체각 격자의 합성 edge 민감도와 기하 셀 proxy | 각 후보의 물체각·파장 → ADC 이전 평균 광전자 국소 전달 응답을 보존하고 유효 footprint를 비교 |
| HDR/LFM | 저장 LFM 평균 전하의 2노출×4위상 시간 적분 교차 검사 | 실제 센서 mode·행 timing·광원·움직임과 fusion coverage 검증; 기존 HDR coverage `inconclusive` 해소 |
| 인지 | 작은 합성 fixture에서 API 연결과 mAP 계산 진단 | 독립 장면·검증된 정답·실제 모델 실행·held-out 평가 |
| ISP·최적화 | 등록 RAW 4개를 쓰는 제한된 noise/detail 탐색과 holdout 재평가 | 범용 ISP 모듈 계약, 여러 장면·목적함수·예산의 최적화 및 재현 가능한 운영 API |

부분 파장 PSF를 Lens 모델로 등록하거나 제품 순위에 연결하는 것은 현 증거로 정당화되지 않는다. 전 파장에 대한 효율적인 위상 적분과 독립 수렴 검사가 먼저 필요하다. 광학 모델이 통과한 뒤에도 렌즈 광량과 센서 각도 응답을 확인하기 전에는 화질 순위를 해제하지 않는다. 실측 정확도와 제품 수준 검증은 미확립이다.

[dev29 wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev29-py3-none-any.whl)의 SHA-256은 `1ebd284444fa47aaa703bdc9052c5e35bd19c5e57feaef6c15a4b72ceca58133`이다. 격리 설치본의 1,040개 파일을 wheel과 바이트 대조한 뒤, 새 Registry·child Worker에서 2×2 RAW와 화질 Job을 실행해 [결과](../reports/camera_platform/integrated-workflows-20260928/joint-dev29-quality-installed-proof.json)를 재조회했다. 새 문구와 공통 각도 metric이 동시에 성립하고 제품 순위 flag가 계속 false임을 확인했다. 소스 화질 테스트 16개와 변경 Python 파일 Ruff, `git diff --check`가 통과했다. 좁은 파장 회절 PSF는 보고서의 소스 계산·저장 배열 독립 검사이며, 설치 Worker나 Lens×Sensor RAW에 등록해 실행한 결과가 아니다.
