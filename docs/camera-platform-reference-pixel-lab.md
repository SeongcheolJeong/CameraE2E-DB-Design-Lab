# Reference Pixel Design Lab

Reference Bench의 검토된 픽셀 구조와 이미 계산한 CFA 후보를 연결하여 **구조 변경 → 분광·입사각 응답 → RAW → 색 보정·잡음**을 한 흐름에서 비교한다. 고정된 특허 렌즈 아래에서 CFA 두께 ×0.9/1.0/1.1을 비교하며, 원래 2.9 µm pitch와 4 ms·gain 1·1000 cd/m²·8회 취득 조건을 유지한다.

## 원본과 비교 범위

기준은 IMX462의 DEF-2011-804 보고서에서 검토한 구조와 모델 가정이다. CFA를 바꿀 때 passivation을 반대로 조정하여 총 높이를 유지한 기존 계산 결과를 사용한다. 재료 n,k·수집효율·readout은 저장 모델의 가정이며 실제 제품 특성으로 표시하지 않는다. 새 FDTD 해석이나 임의 센서 mode를 추가하지 않는다.

광학은 US10310255B2 p0014의 고정된 기하 raytrace 응답이다. 각 후보는 중심·중간부·주변부의 9개 독립 탐침에서 256×256 native RAW를 갖는다. 입사각 응답과 기존 동공 방향 적분을 재사용하며, 전체 제품 센서 프레임이나 회절 검증으로 해석하지 않는다.

같은 CFA 안에서 spectral CCM과 Munsell64 재피팅 CCM은 정확히 같은 RAW를 사용한다. 다른 CFA 후보 사이에서는 픽셀 응답 변화가 실제 광전자와 RAW에 반영되어 있으므로, 이를 동일 RAW라고 부르지 않는다.

## 고정한 source

| 용도 | Run | Candidate |
| --- | --- | --- |
| CFA 0.9/1.0/1.1 취득 | `run-c07bf344f52848be9727c122ccde8257` | 각각 `candidate-00/01/02` |
| CFA ×0.9 색 보정 | `run-e6c237d2a04d432b84f34cbc3a7f27c5` | `candidate-00` spectral / `candidate-01` Munsell64 |
| CFA ×1.0 색 보정 | `run-b68d3643ffa14ca5886489e3bb989308` | `candidate-00` spectral / `candidate-01` Munsell64 |
| CFA ×1.1 색 보정 | `run-36d939ce2be742cfa2caa9a41f7cd53c` | `candidate-00` spectral / `candidate-01` Munsell64 |

Manifest SHA-256:

```text
acquisition  9a39aa3d5f568ac33c6b097e2506ff468938f68d2f4a8837d29a8105f7b73255
color ×0.9   3de5872f01b0d0be5398411ef033dfce94f9ad82bec3cb83079acc7b89ea47aa
color ×1.0   efe2c0fcb58687f7b5ee42c82ccd84c550c32f322ac03479f17ec0f83b0113c6
color ×1.1   8aaf1daad0036b8853c238137bc2feba08e3a6d0784e62ba4b1cd39e10bd9055
```

×1.0의 색 study는 별도 기준 run `run-34aadb199e4d416bb0ba390ccf79636c`를 원본으로 기록한다. 이 run은 sweep의 ×1.0 candidate와 같은 취득 설정·RAW 내용을 갖는다. 원본 run ID가 다르다는 이유로 연결을 거부하지 않으며, 실제 취득·RAW·평균 응답 hash의 일치로 결합한다. 준비 단계에서 CFA별 288개 RAW hash와 36개 평균 응답 hash가 두 색 처리 후보와 모두 일치하는지 확인한다.

## 저장·재실행 원칙

원본은 `/Volumes/T7/CameraE2E/reference-workshop`에 그대로 둔다. Pixel Lab 작업은 source manifest와 필요한 artifact를 참조하며 원본 RAW를 다시 촬영하거나 새 폴더에 복사하지 않는다. 결과 패키지는 명세·비교 수치·출처·보고서를 보존한다. 원본 hash가 달라졌거나 저장소가 없으면 계산한 것처럼 채워 넣지 않고 실패 이유를 남긴다.

공개 제출 API는 `POST /api/v1/reference-workshop/pixel-studies`이며 SDK의 `submit_reference_pixel_study(specification)`을 사용한다. 조회·대기·취소·bundle은 기존 Reference Workshop lifecycle을 공유한다. 새로운 capture job과 source 비교 작업을 구분한다.

## 세 가지 데모

1. **구조와 광자 예산:** CFA 두께, 이를 보상하는 passivation, 수집 전자와 손실을 함께 확인한다. 반사·각 층 흡수·심부 투과의 합과 Si 흡수의 수집/손실 분해를 표시한다. Si 흡수와 그 하위 수집 항목을 이중 합산하지 않는다.
2. **렌즈와 픽셀의 각도 결합:** 같은 센서면 광자에 대한 normal/chief/pupil 응답을 비교한다. 저장된 방향별 광자 가중치를 쓰며 cosine·면적·광량을 다시 곱하지 않는다. 세 응답은 진단 결과이며 세 번의 신규 촬영이 아니다.
3. **픽셀과 색 보정의 상충관계:** 같은 CFA·RAW에서 spectral CCM과 Munsell64 CCM의 색차와 시간 잡음을 나란히 본다. CFA 간 비교에서는 QE·광전자 변화와 색 보정 효과를 구분한다. 지원 마스크가 부족한 지표를 우수 후보의 근거로 삼지 않는다.

현재 모델은 평면층·방위각 독립 응답이다. Cross-section의 microlens 모양이 새 3D 해석을 뜻하지 않으며 DTI·공간 crosstalk 개선을 주장하지 않는다. 각도 지지 범위가 부족한 시야는 `unavailable`과 이유를 표시한다. 후보·시야·파장·사이트·색 모드를 고른 후 수동 비교를 저장하며 자동 제품 순위를 만들지 않는다.

## SDK와 한 번의 준비 실행

서버 template와 패키지의 `data/reference_pixel_study.json`은 동일한 세 후보 및 네 source manifest를 고정한다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=120) as client:
    spec = client.reference_pixel_studies()["template"]
    job = client.submit_reference_pixel_study(spec)
    done = client.wait_reference_workshop(job["id"], timeout=7260)
    if done["status"] != "succeeded":
        raise RuntimeError(done.get("error") or done["status"])
    details = client.reference_pixel_candidate(job["id"], "candidate-00")
    comparison = client.save_reference_pixel_comparison(job["id"], {
        "name": "CFA 0.9 versus 1.0 · outer NE",
        "candidate_id": "candidate-00",
        "baseline_candidate_id": "candidate-01",
        "field_id": "outer-ne",
        "wavelength_nm": 550,
        "site": "Gr",
        "color_mode": "same_cfa",
        "story": "photon_budget",
    })
```

취소는 `client.cancel_reference_workshop(job["id"])`를 사용한다. 완료 전에 저장 비교를 요청하면 거부한다. 보고서와 bundle URL은 완료 결과 및 저장 비교 응답에 포함된다. Bundle에는 진단 JSON·그림·보고서와 source 참조만 들어가며 기존 RAW는 포함하지 않는다.

```bash
PYTHONPATH=src .venv-platform/bin/python tools/run_reference_pixel_lab.py
```

실행기는 같은 명세·코드의 완료 결과를 재사용한다. 기다리는 동안 제한시간이 지나도 서버 작업을 완료로 표시하지 않으며 재실행하면 기존 job을 다시 조회한다. `--comparison-spec <JSON>`으로 위 저장 비교 사양을 전달할 수 있다. 결과는 기본적으로 `outputs/reference-pixel-lab-delivery-20261011/`에 기록한다.

- `specification.json`, `runs.json`: 정확한 source와 제출·완료 receipt.
- `result.json`, `verification.json`: 파일 해시·27개 시야 진단·manifest·실제 저장량과 Registry audit 기반 대기/실행 시간.
- `comparison.json` (선택), `delivery.json`: 저장 비교와 데모 링크.

취득·색 source 파일 전체는 worker가 검증하고, 준비 실행기는 신규 패키지 모든 파일과 source manifest를 독립 확인한다. 실행 벽시계 시간은 해시 검사·진단·보고서 저장 시간을 포함하며 센서/ISP의 하드웨어 지연을 의미하지 않는다.

## 인도 기록

2026-10-11, `run-ef087598619c4108a03f06716aae2c73`가 공통 SDK와 worker를 거쳐 완료했다. 준비 실행기의 manifest·후보·시야·코드·audit 검증도 `verified`이다.

- [Pixel Design Lab](http://127.0.0.1:8780/reference-workshop.html?pixel_study=run-ef087598619c4108a03f06716aae2c73&pixel_candidate=candidate-00&field=outer-ne)
- [계산 보고서](http://127.0.0.1:8780/api/v1/reference-workshop/runs/run-ef087598619c4108a03f06716aae2c73/files/report.html)
- [참조 패키지](http://127.0.0.1:8780/api/v1/reference-workshop/runs/run-ef087598619c4108a03f06716aae2c73/bundle)

| 확인한 항목 | 실제 결과 |
| --- | --- |
| 후보·시야 | 3개 CFA × 9개 시야 = 27개, 모두 available |
| 신규 저장 | manifest 내 9개 파일, 논리 5,692,999 bytes, 할당 6,422,528 bytes |
| 신규 RAW 복사·취득 | 각각 0 |
| 대기·실행 시간 | 대기 0.255초, 실행 58.892초 (원본 파일 검증·진단·보고서 저장 포함) |
| 결과 manifest SHA-256 | `dd5bea812785c3b08fa99596a9eab54f1e9ee42aa030dc3894960698a906be59` |

아래는 outer-ne의 실제 저장 지표다. QE는 550 nm Gr의 동공 적분 값이며 색 지표는 같은 CFA·RAW에서 spectral CCM → Munsell64 CCM으로 바꾼 결과다.

| CFA 두께 | 동공 적분 QE | 차트 평균 ΔE00 | 색 차트 시간 잡음 Y 변화 |
| --- | --- | --- | --- |
| ×0.9 | 0.484027 | 3.7102 → 2.5510 | −2.46% |
| ×1.0 | 0.477779 | 2.4398 → 2.5684 | +0.98% |
| ×1.1 | 0.471614 | 3.1227 → 2.5826 | +4.88% |

×0.9에서는 재피팅이 평균 색차와 잡음을 모두 낮췄지만 개별 색 패치의 악화도 남는다. ×1.1에서는 평균 색차가 줄면서 잡음이 늘었고, ×1.0에서는 기존 spectral CCM이 이 두 평균 지표에서 유리했다. 재피팅을 일괄 개선으로 표시하지 않는다.

기준 CFA ×1.0의 outer-ne chief 각도는 18.72°다. 같은 파장·사이트의 normal/chief/pupil QE는 각각 0.478914/0.477827/0.477779다. 이 렌즈·층 모델에서는 각도 차이가 작다는 결과도 그대로 보인다. 이를 넓은 화각 전체나 실제 제품의 광학 성능으로 확대하지 않는다.

검증 및 receipt는 `outputs/reference-pixel-lab-delivery-20261011/{verification,runs,delivery}.json`에, 실제 패키지는 `/Volumes/T7/CameraE2E/reference-workshop/run-ef087598619c4108a03f06716aae2c73`에 있다. HTML에는 과학 그래프가 내장되어 있고, 원본 source를 여는 링크에는 실행 중인 플랫폼 서버가 필요하다.

실제 8780 브라우저에서 세 스토리, 중심/outer-ne 전환, 256×256 원본 영상,
파장 변경 시 D65 영상 불변, 동공 가중치 합, 색 보정 전후 동일 RAW를 확인했다.
`reference_pixel_comparison-10313462080640d78755f09b08c38598`에 CFA ×1.1의
outer-ne 색 재보정 비교를 저장하고 JSON·보고서 재조회까지 확인했다.
`ui-verification.json`과 `api-verification.json`에 화면·원본 다운로드·ZIP 해시 검증을 기록했다.
별도 조회에서 작은 목록은 0.007초, 선택 후보 상세는 0.097초가 걸렸다.
Python 검사 39개와 브라우저 VM 검사 3개가 통과했으며, 실제 화면의 콘솔 오류는 없었다.

색 calibration의 학습/보류 분리와 유효 마스크, 단위·고정 표시 범위는 [Reference ISP Lab](camera-platform-reference-isp-lab.md)을 따른다. 원자료의 이용 조건과 특허·구조 근거는 [Reference Bench](camera-platform-reference-workshop.md)에 기록되어 있다.
