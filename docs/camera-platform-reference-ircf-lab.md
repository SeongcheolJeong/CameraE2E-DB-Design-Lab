# Reference IRCF Design Lab

IRCF 투과 대역·입사각이 같은 렌즈와 센서의 광전자, 정수 RAW, 색에 미치는 영향을
Reference Bench에서 비교한다. 필터 변경은 새로운 광학·센서 계산과 8회 RAW 취득이며,
기준 CCM과 필터별 CCM은 그 보존 RAW를 함께 사용한다.

## 실행

기존 8780 서버의 `/reference-ircf.html`에서 준비 결과를 선택하거나 새 계산을 제출한다.
Reference Bench 상단의 **IRCF Lab**도 같은 화면으로 연결된다.

```sh
PYTHONPATH=src .venv-platform/bin/python tools/run_reference_ircf_lab.py --scope first_pair
PYTHONPATH=src .venv-platform/bin/python tools/run_reference_ircf_lab.py --scope full
```

첫 A/B는 중심·DAY·Macbeth의 기준 필터/1% 누설 필터 2개 조건과 16장 RAW다.
전체 비교는 중심/outer-ne × DAY/A × Macbeth/균일장 × 4필터의 32개 조건과
주변부 기준 필터의 각도 독립 진단 2개 조건, 총 **34개 조건·272장 RAW**다.
모든 RAW는 native 256×256, RGGB, 4 ms, gain 1, 25°C, 1000 cd/m²다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=180) as client:
    spec = client.reference_ircf_studies()["template"]
    job = client.submit_reference_ircf_study(spec)
    result = client.wait_reference_workshop(job["id"])
    if result["status"] != "succeeded":
        raise RuntimeError(result.get("error", result["status"]))
    case = client.reference_ircf_case(job["id"], "case-000")
```

조회·대기·취소는 공통 Reference 작업 lifecycle을 사용한다. 서버가 재시작되더라도
완료한 결과는 보존된다. 실패한 작업을 완료로 바꾸거나 해상도를 낮추는 fallback은 없다.
실패 작업의 개별 프레임을 재개하는 기능은 이번 adapter에 포함하지 않는다.

## 시연 순서

1. **필터 차이 · CCM 고정**: 같은 위치·조명에서 기준과 NIR 누설 또는 cutoff ±20 nm를
   비교한다. 파장별 전자 기여, 채널별 전자 수, 시간 잡음, Macbeth ΔE00를 함께 본다.
2. **각도 효과 · 기준 필터**: outer-ne 균일장에서 각도 독립과 실제 동공 방향 가중을
   비교한다. 센서 QE의 각도 반응은 양쪽 모두 유지하고 필터의 각도 효과만 바꾼다.
3. **같은 RAW · CCM만 변경**: 필터를 고정하고 중심 D65/Munsell로 재보정한 행렬을
   비교한다. Macbeth 평가 입력과 주변부 출력은 fitting에 사용하지 않는다.
4. **같은 보정 방법 · 필터 변경**: 각 필터의 Munsell CCM을 적용한 비교로 바꾼다.
   가시광 fitting이 NIR 오염을 모두 제거한다고 가정하지 않는다.

선택한 case·CCM·표시 단계를 프로필로 저장할 수 있다. 설정 입력 변경만으로
결과가 바뀌지 않는다. 제출 뒤 새 실행이 완료되어야 새 영상과 수치를 보여준다.

## 계산 계약

- 400–1000 nm, 기본 10 nm와 명시적 5 nm refinement. 동일 구간 사다리꼴 적분.
  기존 Reference의 450–650 nm/10 nm 계약은 변경하지 않는다.
- 확장 파장 범위에는 257×257 기하 PSF와 512×512 주변 입력을 사용하고 가운데
  native 256×256을 취득한다. 최초 129×129 시도는 400 nm 부근 광선 지지 범위가
  잘려 거부되었다. 광선 에너지 99.9% 유지 기준은 낮추지 않았다. 커진 커널로
  계산 격자·sensor pitch·FOV가 바뀌는 것은 아니다.
- 기존 특허 처방을 새 파장축에서 기하 광선 추적한다. 필터는 센서와 평행한 마지막
  공기층의 무두께 투과 mask다. 동공의 원래 광자 가중치로 `T × absolute QE`를
  적분하며 통과한 광선의 가중치를 다시 1로 정규화하지 않는다.
- 필터 투과 원본은 로컬 `ircf_public.mat`. cutoff 변경은 600–620 nm에서 부드럽게
  시작해 장파장 쪽에만 적용한다. 누설은 700–750 nm에서 증가해 750 nm 이상 1% floor.
- 각도 이동은 선언한 유효 굴절률 1.8을 사용하며 1.4–2.5에서 설정 가능하다.
  이 수치는 제품 코팅에서 측정한 값이 아니다. 투과율만으로 흡수와 반사를 구분하지 않는다.
- 400–700 nm는 기존 구조 기반 센서 응답이다. 그 이상의 QE는 700 nm 각 채널값에서
  800/900/1000 nm의 공통 0.20/0.12/0.02 앵커로 연결한 **NIR proxy**다.
  앵커 배율 0.5/1/2를 선택할 수 있고 700 nm의 상대 각도 응답을 NIR에 유지한다.
- 새 광대역 baseline 필터에서 만든 중심 CCM·시야별 LSC를 고정한다. 과거 450–650 nm
  calibration을 넓어진 파장축에 무심코 재사용하지 않는다. 필터별 CCM은 중심
  D65 가시광 Munsell64에서 white constraint를 적용해 fitting한다.
- LSC는 한 번 적용하며 분산에는 gain²을 적용한다. 동일 bilinear demosaic,
  수동 WB=1, 표시 white=1, gamma=2.2를 사용한다. 자동 밝기/WB·NR·sharpen은 없다.
- 전자 잡음은 8회 취득의 픽셀별 시간 분산이다. 처리 후 RGB 잡음은 signed linear
  sRGB 단위이며 전자로 표시하지 않는다. 포화·무효 평가 패치는 제외 개수를 함께 남긴다.

## 보존·지원 범위

결과는 외장 SSD `/Volumes/T7/CameraE2E/reference-workshop/<run-id>`에 저장된다.
기존 500 GB 프로젝트 예산과 최소 여유 용량 검사를 유지한다. 매 case의 `capture.npz`,
`arrays.npz`, `readout.json`, `case.json`, RAW/ISP 미리보기, 공통 calibration·LSC·광학
응답·분광 자산 출처와 해시를 manifest로 묶는다. report 및 bundle은 공통 파일 API로
다운로드한다. 원본 Reference 취득 파일은 변경하지 않는다.

로컬 Munsell은 400–700 nm만 지원한다. NIR 반사율을 만들어 fitting하지 않는다.
DAY는 로컬 광대역 DAY 자산이며 D65로 부르지 않는다. 색 목표는 광대역 Macbeth와
해당 광원의 CIE XYZ이며 프레임마다 밝기를 정규화하지 않는다.

색 matching 함수는 CIE 1931 공식 자료의 400–830 nm/5 nm subset을 사용한다.
CIE 2019, DOI `10.25039/CIE.DS.xvudnb9b`, **CC BY-SA 4.0**의 출처·원본 해시·가공 정보를
`spectral-assets.json`에 보존한다. 다른 로컬 입력의 출처·권리는 그 파일의 provenance와
구분한다. 이 구현은 모든 DB/자산의 외부 재배포 권한을 새로 부여하지 않는다.

기하 PSF와 동공 분광 가중은 분리된 근사다. 필터 때문에 달라지는 PSF 형상, 굴절·초점
이동, 편광, 반사 ghost, 실측 NIR 유리 투과율은 포함하지 않는다. 결과는 모델 내부
신호·색 민감도이며 실제 IMX462의 NIR 성능이나 필터 제품 검증이 아니다.
5 nm 실행 옵션이 있다는 사실만으로 파장 수렴이 입증되지는 않는다. 대표 입력의
refinement 차이는 실제 실행 결과에 별도로 기록한다.

## 2026-10-12 준비된 실제 데모

전체 실행은 `run-79018953986b4a64b6e37f125de78c2d`다. 34조건에서 native RAW 272장을
생성했다. 광학 캐시 재사용 후 비교 계산은 703.59초, manifest 파일의 논리 크기는
986,987,861 bytes, 실제 할당량은 1,006,632,960 bytes였다. 최초 광학 계산 시간은 이
703.59초에 포함하지 않는다. 준비 과정의 129 PSF 실패와 확대 후 계산 근거도 보존했다.

- [A광원에서 신호와 NIR 색 오염](http://127.0.0.1:8780/reference-ircf.html?run=run-79018953986b4a64b6e37f125de78c2d&case=case-008&a=baseline&b=nir_leak_1pct&ccm_a=baseline&ccm_b=baseline)
- [같은 RAW의 색 재보정](http://127.0.0.1:8780/reference-ircf.html?run=run-79018953986b4a64b6e37f125de78c2d&case=case-010&a=cutoff_plus20&b=cutoff_plus20&angle_a=pupil&angle_b=pupil&ccm_a=baseline&ccm_b=refit)
- [주변부에서 IRCF 입사각 효과](http://127.0.0.1:8780/reference-ircf.html?run=run-79018953986b4a64b6e37f125de78c2d&case=case-028&a=baseline&b=baseline&angle_a=independent&angle_b=pupil&ccm_a=baseline&ccm_b=baseline)
- [전체 계산 보고서](http://127.0.0.1:8780/api/v1/reference-workshop/ircf-studies/run-79018953986b4a64b6e37f125de78c2d/files/report.html)

중심·A광원·Macbeth에서 기준 대비 1% 누설의 평균 광전자는 775.23→830.55 e⁻
(+7.14%), NIR 기여율은 0.282→7.020%, 평균 ΔE00는 7.218→11.590이다.
누설 변경은 가시광 fitting 입력을 바꾸지 않으므로 CCM refit으로 이 오염을 제거하지 않는다.
장파장 차단 +20 nm의 같은 RAW에 필터별 CCM을 적용하면 ΔE00 16.305→9.201로
개선되지만 기준 필터의 7.218에는 미치지 못한다. 모든 색 조건은 24/24패치가 유효했고,
모든 조건에서 well/ADC 포화가 없었다.

10→5 nm 비교는 중심/outer-ne × DAY/A × 기준/누설의 8조건에 대해 독립적으로
계산한 5 nm 광학 자산을 사용했다. 균일장 site 전자량의 최대 변화는 0.09853%,
개별 Macbeth patch/site의 최대 변화는 1.23993%, patch 색 예측 사이의 최대 ΔE00는
0.25334였다. 기준/누설 색차 순서가 바뀐 조건은 없었다. 독립 균일 패치의 평균 응답
진단이며 공간 차트 경계·MTF·잡음/포화·제품 모델의 수렴을 증명하지 않는다.
DAY의 작은 필터 차이를 일반적인 성능 우위로 해석하지 않는다.

새 연결과 기존 관련 회귀 114개, UI 상태 fixture, 실제 브라우저 A/B·CCM·각도·저장
흐름을 확인했다. 저장 결과는 247개 파일/원본 해시와 68개 첫 프레임 ISP 재현을
별도로 확인했다. 기록은 `outputs/reference-ircf-lab-20261012/`의 `findings.json`,
`refinement.json`, `*-verification.json`, `full-10nm-nir1-delivery.json`에 있다.
