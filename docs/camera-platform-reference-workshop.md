# 특허 렌즈 × TechInsights Reference 설계 워크숍

`/reference-workshop.html`은 기존 단렌즈 워크숍과 별도로, **고정된 특허 처방과 검토된 센서 구조**를 기준으로 설계 변경을 비교한다. 제품 센서의 전체 영상 대신 원래 pitch를 유지하는 가상 시야 탐침을 사용한다.

ISP 확장 데모와 동일 RAW 재처리·색 재보정·6후보 선택의 현재 사용법은 [Reference ISP Lab](camera-platform-reference-isp-lab.md)을 따른다.

## 기준과 실제 계산 범위

- 렌즈: US10310255B2, p0014 intermediate, revision `6c48e967-74b4-5901-8186-ba6fafe4797b`. 검토 CSV와 DB revision의 일치·해시를 확인한다. 특허 유효직경을 clear aperture로 해석하고 nd/Vd 분산을 근사하는 기존 계약을 유지한다.
- 픽셀: DEF-2011-804, IMX462 기반 **reviewed_techinsights** 구조 overlay, 원래2.9µm pitch. 보고서의 CFA0.45–0.55µm 관측 범위와 색별0.5µm 할당 가정을 구분한다. 재료 n,k·수집효율·readout은 공통 모델 가정이며 실측 특성이 아니다.
- 9개 탐침: 중심, 정규화 image height0.5의 상하좌우,0.85의 대각선. 각256×256 native ROI에 PSF halo를 별도로 생성한다. 전체 이미지의 crop이라고 주장하지 않는다. 각 탐침은 로컬 RGGB 원점[0,0]을 갖는다.
- 기본 입력:450–650nm/10nm, D65,4ms,gain1,8회 독립 readout. 밝은 경우10000cd/m²/white level10, 저광량1000cd/m²/white level1. 설정은 촬영 전에 고정한다.

### 회절과 기하 PSF를 분리하는 이유

현재 scalar wavefront backend의 reference-axis 적용 범위는2°다. 이 특허 렌즈의 중간·주변부는 그 범위를 벗어나므로, 기존 gate를 풀어 전체 시야 회절 계산으로 표시하지 않는다.

기본 `geometric_retrace`는 **수정한 실제 처방을 RayOptics로 다시 추적**하고, 수신 픽셀별 광선 질량을 한 번 적분한 기하 PSF를 사용한다. 저장 PSF에 임의 blur를 더하는 방식이 아니다. 분광 횡색수차는550nm 기준 chief 위치에 대한 파장별 착지 위치로 보존한다. 축대칭 처방의 세 반경에서 얻은 광선을 실제 방위각으로 회전해9개 탐침을 구성한다. 입사 광량·동공 방향 가중치는 별도 reciprocal radiometry에서 계산한다.

이 프로필은 회절을 포함하지 않으며, 기하 PSF의 동공 표본 수렴이 인증된 결과도 아니다. `wavefront` 선택은 기존 제한을 유지하고 실패 이유를 반환한다. 회절 실패를 기하 PSF 성공으로 바꾸는 자동 fallback은 없다.

각 ROI는 해당 시야의 local shift-invariant PSF/RI 근사다. 탐침 중심 위치와 왜곡은 기록하지만 ROI 내부의 완전한 기하 warp·전 시야 영상·landing-position과 입사각의 결합 수송은 구현 범위가 아니다.

## 설계 변경과 시연 순서

1. Reference 카드에서 원문 근거·공통 가정·변경값, 렌즈 면과 센서 층을 확인한다.
2. 기준안과 최초 유한 곡률면의 곡률×1.005를 비교한다. 같은 물체 방향의PSF·MTF·CRA·RI·RAW를 본다. 곡률 배율이므로 반경에는 역수를 적용한다.
3. 곡률0.995/1/1.005 × 센서Z −20/0/+20µm의3×3 후보를 비교한다. 자동으로 승자를 선정하지 않는다.
4. CFA0.9/1/1.1배에서 passivation을 반대로 바꿔 총 높이를 유지한다. 새 절대 QE가 이전 QE를 대체한다. OCL/DTI 공간 해석은 추가하지 않는다.
5. **같은 RAW**에서 고정LSC와LSC+denoise를 비교한다. 균일장·색·edge·texture에 대해 밝기 복원과 시간 잡음·질감 손실을 동시에 확인한다.

LSC는 기준 렌즈·기준 픽셀의 잡음 없는 균일장에서 같은 CFA 사이트의 중심 평균을 기준으로 한 번 계산한다. 최대gain4, 후보별 재피팅 없음. 색 변환도 기준 중심의 실제 동공 평균 응답에서 한 번 고정한다. 최대 밝기나 후보별 백색을 맞춰 결과를 정규화하지 않는다.

### 시연 광량과 포화의 해석

주 시연은 **1000 cd/m², 4 ms, gain 1, display white 1** 비교를 사용한다. 동일한 설정에서 렌즈 곡률, CFA 두께, 같은 RAW의 LSC·denoise를 각각 바꾼다. 10000 cd/m²와 display white 10 결과는 밝은 조건의 스트레스 사례로 별도 보존한다.

실제 초기 실행에서 10000 cd/m² 기준안의 중심 색 차트는 well 포화 약 31%를 보였다. 이 조건의 ΔE는 clipping 영향도 포함하므로 CFA의 분광 선택성만으로 해석하지 않는다. 색차·SFR과 함께 해당 차트의 well/ADC 포화율을 확인한다. 포화가 생겼다고 노출·gain·출력 범위를 자동으로 바꾸지는 않는다.

## 실행·API·저장

기존8780서버, Registry, Worker, 공통SDK wait/cancel을 그대로 사용한다. 새 namespace는 `/api/v1/reference-workshop`이다. `/catalog`, `/revisions`, `/runs`, `/sweeps`, `/replays`, `/runs/{id}/files/...`, `/runs/{id}/bundle`을 제공한다. 명세는 `camera_reference_workshop_v1`이며 `reference_id`, lens의 `curvature_scale`·`sensor_shift_um`·`optical_profile`, pixel의 `cfa_thickness_scale`, 취득 조건과 ISP를 고정한다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780") as client:
    spec = client.reference_workshop_catalog()["specification"]
    spec["luminance_cd_m2"] = 1000
    spec["isp"]["display_white_level"] = 1
    job = client.submit_reference_workshop(spec)
    result = client.wait_reference_workshop(job["id"], timeout=7260)
    isp = {**spec["isp"], "lsc_enabled": True}
    replay = client.replay_reference_workshop(job["id"], isp)
    compared = client.wait(replay["id"], timeout=7260)
```

준비된 비교 묶음 실행:

```bash
PYTHONPATH=src .venv-platform/bin/python tools/run_reference_workshop_demo.py --lens-sweep
```

결과는 `/Volumes/T7/CameraE2E/reference-workshop`, 캐시는 같은 프로젝트의 `reference-workshop-cache`에 저장한다. 기존 작업실과 공통인 500GB 예산과 단일 쓰기 잠금을 사용한다. 용량 부족이나 계산 실패 때문에 해상도를 낮추지 않는다. 원본 DB는 변경하지 않는다.

비교 ZIP에는 보고서 원문이나 원본 단면 이미지를 자동으로 포함하지 않는다. 파생 수치의 이용 권한을 새로 부여하는 기능은 아니며, 원자료의 내부 연구용 출처 조건을 유지한다. 계산 완료·물리 검증·외부 재배포 가능 여부는 구분한다.


## 바로 여는 실제 데모

| 순서 | 비교 화면 | 보여줄 설계 질문 |
| --- | --- | --- |
| 1 | [특허 처방 곡률 A/B](http://127.0.0.1:8780/reference-workshop.html?run=run-ea22df19917640018219cf50a394e444&baseline=run-34aadb199e4d416bb0ba390ccf79636c&field=outer-ne) | 중심부 전달이 좋아져도 주변부 tangential 전달은 손실될 수 있는가? |
| 2 | [CFA 두께 후보](http://127.0.0.1:8780/reference-workshop.html?run=run-c07bf344f52848be9727c122ccde8257&candidate=candidate-02&baseline=run-34aadb199e4d416bb0ba390ccf79636c&field=outer-ne) | 같은 pitch·총 높이·readout에서 CFA 변경이 전하와 색차를 어떻게 바꾸는가? |
| 3 | [같은 RAW의 LSC](http://127.0.0.1:8780/reference-workshop.html?run=run-dc987104ec054de1b158efe19cfe903c&baseline=run-34aadb199e4d416bb0ba390ccf79636c&field=outer-ne) | 주변부 밝기·색 복원과 잡음 증가의 관계는 무엇인가? |
| 4 | [LSC와 LSC+denoise](http://127.0.0.1:8780/reference-workshop.html?run=run-cbdb6ba5d2804c58a97c37ea23c0bb08&baseline=run-dc987104ec054de1b158efe19cfe903c&field=outer-ne) | 잡음을 줄이면서 잃는 질감은 얼마나 되는가? |

1000 cd/m²의 실제 주변부 NE 결과에서 LSC는 ΔE00 7.982→2.448, 처리 후 시간 잡음 0.004673→0.006928을 보였다. Denoise를 추가하면 잡음은 0.002963으로 줄지만 질감 대비 비율은 0.5909가 된다. 숫자는 고정된 8회 취득·차트·모델의 결과이며 제품 사양이 아니다.

광학 곡선은 수신 픽셀 면적을 적분한 550 nm 기하 PSF의 전달 진단이다. 특허 처방에 적용한 분산·이미지면 가정을 포함하므로, 실제 상용 렌즈를 개선했다는 결론으로 해석하지 않는다. ISP의 edge SFR·overshoot와 이 광학 곡선은 구분한다.

재실행 결과와 검증 기록은 `outputs/reference-workshop-delivery-20261011/runs.json`, `verification.json`에 남는다. 기본 실행기는 완료된 작업을 재사용하고 마지막에 원본·결과 관계를 검증한다. 브라우저 확인 기록은 `outputs/reference-workshop-ui-20261011/results-qa.json`이다.


## 계산·저장 성능과 완료 기록

- 실제 실행: 11개 작업 묶음, 23개 설계·재처리 케이스, 207개 성공 시야 결과.
- [곡률 × 센서 Z 3×3 비교](http://127.0.0.1:8780/reference-workshop.html?run=run-73f1b500e79c40dfa2a8eadba9eef960&candidate=candidate-07&baseline=run-34aadb199e4d416bb0ba390ccf79636c&field=outer-ne): 9개 후보 모두 9개 시야의 RAW·ISP 계산 완료.
- 원본·설정 관계 검사 186개 통과. Manifest의 33,351개 파일, 6,624개 정수 RAW 배열을 확인했다. RAW 수에는 재처리 패키지에 보존한 동일 RAW 사본도 포함되므로 독립 촬영 횟수로 해석하지 않는다.
- 파일의 논리 용량은 5.43GB, SSD 할당 용량은 9.46GB다. 이는 이번 결과 manifest 범위이며 광학 캐시와 기존 작업실 결과는 제외한다.
- 처음 광학 응답을 생성할 때 약 4분/후보가 필요했다. 캐시를 사용한 3×3 실행의 RAW·ISP 단계는 약 48–64초/후보였다. 실시간 계산이라고 표시하지 않으며, 준비된 결과 선택과 새 계산을 구분한다.
- 기능·회귀 테스트 163개와 브라우저의 실제 결과 연결을 확인했다. 물리 정확도 인증 또는 실제 제품의 성능 보증은 아니다.
