# DB Lens × Sensor Design Lab

기존 Reference 외의 특허 처방과 검토된 센서 구조를 같은 native RAW·ISP 계산에 연결한다.
실행 성공, 출처 확보, 실제 제품 정합성은 별도 상태다. 데모는 제품 순위를 만들지 않는다.

움직이는 대상의 촬영 조건은 [광량·속도별 조리개·노출 공동 선택](camera-platform-aperture-motion.md)에서
비교한다. 보존된 회절 응답에 노출 중 이동을 적분하고 새 RAW·ISP를 취득하며,
정적 조리개 탐색 결과와 별도 실행으로 보존한다.

## 사용할 수 있는 모델

- 기준 렌즈: Canon US10310255B2 p0014 intermediate.
- 추가 렌즈: Canon US20200174235A1 p0248 wide-angle.
- 기준 센서: 검토된 IMX462 구조 / 공통 가정 readout.
- 추가 센서: 저장된 IMX662 또는 IMX307 physical_estimate_v1 snapshot.
- IMX662의 readout은 ASI662MC 공개 카메라 자료 기반 proxy다. 개별 실리콘 실측이 아니다.

센서들은 2.9 µm, RGGB 네 사이트, 공통 파장·각도 범위를 지원한다. 다른 pitch나 CFA를
자동 변환하지 않는다. 카탈로그 revision, 처방 CSV, 센서 저장 run과 파일 해시를 고정하며
제출 전후에 확인한다. 원본 DB는 덮어쓰지 않는다.

## 계산 및 비교 조건

D65 1000 cd/m², 4 ms, gain 1, 8회 독립 취득. 중심과 outer-ne에서 각각 256×256 native
RAW를 생성한다. 렌즈 2개 × 센서 2개 × 시야 2개 × 차트 4개 × 반복 8회 = 256 RAW.
해상력·Macbeth·균일장·dead-leaves 입력, 평균 전자율, 정수 RAW, 선형/표시 ISP가 보존된다.

기존 처방 추적과 450–650 nm/10 nm 응답, 동공 방향 적분 및 절대 픽셀 QE를 재사용한다.
PSF 동공 표본과 reciprocal 적분 허용오차를 유지하고 계산 시야를 두 곳으로 한정한다.
p0248은 좁아진 주변 동공을 위해 역방향 격자를 40×28에서 28×48로 재배분한다.
총 표본은 늘리고 기존 계산 예산은 유지하며, 각 파장 배치의 수렴 상태를 보존한다.
수신 픽셀 개구는 한 번만 적용한다. 기본 기하광학 프로필은 회절·동공 표본 수렴을
주장하지 않는다. 별도 조리개 회절 비교는 아래의 명시적 프로필을 사용한다. 실측 정합은
두 경로 모두 확보되지 않았다.

Canon p0248의 새 계산은 특허 광각 조건 **F/4.12 @ 587.6nm**를 사용한다. 특허 표의
Effective aperture 19.96mm를 직경으로 해석한 기존 모델은 F/2.751 @ 550nm였고,
이를 촬영 조리개로 그대로 쓰는 것은 특허 광각 F-number와 맞지 않았다. 새 모델은
조리개 직경 13.311592mm를 유도하고 PSF·역방향 광량 계산에 동일하게 적용한다.
원본 표와 해시는 보존하며, [원인과 수정 근거](camera-platform-patent-fnumber-correction.md)에
구분을 기록했다. 기준 p0014의 기존 조건은 유지한다. clear aperture와 nd/Vd 분산은
연구 가정이며 실제 제품 정합성 검증으로 확대하지 않는다.

공통 Reference 중심 스펙트럼으로 만든 전자→색 행렬을 모든 후보에 고정한다. 프레임별
정규화·새 센서별 색 fitting을 하지 않는다. LSC·NR·sharpen은 OFF, Adaptive Laplacian과
동일 tone/white를 사용한다. 센서별 full-well/ADC 값이 달라도 색 변환은 전자 기준이다.
저장 센서의 PRNU/DSNU는 자료에 남지만 공통 FPN OFF 정책을 적용한다. DSNU e/capture를
dark-current DSNU e/s로 잘못 바꾸지 않는다.

두 렌즈는 같은 물체 방향의 시야를 비교하지만, 차트는 같은 native sensor-plane 크기다.
초점거리가 달라 각 ROI의 FOV·물체각 크기는 다르다. 화면은 FOV를 따로 표시하며 이 결과를
동일 물체 크기의 검출 성능이나 완제품 순위로 환산하지 않는다. MTF는 수신 픽셀 적분된
기하 PSF 진단(기본 프로필), SFR은 ISP 처리 후 edge 진단이다. 회절 비교에서는
실제 사용한 수차 포함 회절 PSF와 기하·OPD=0 진단을 구분한다.

## 실행

8780 서버의 `/db-design.html`에서 후보를 선택하고 **새 RAW·ISP 계산**을 제출한다.
`A 기준 실행`을 선택하면 과거 실행과도 비교한다. `run`은 B, 선택적인 `baseline_run`은
A의 실행 ID이며, 각 영상·RAW·지표·다운로드·해시는 해당 실행에 따로 연결된다.
선택/확대는 보존 결과 탐색이며 재촬영이 아니다. 진행·취소·오류는 기존 작업 lifecycle을
사용하고, 실패나 취소를 완료 데이터로 등록하지 않는다. 외장 SSD의 500 GB 프로젝트
예산 및 여유 공간 확인을 유지하며, 부족하면 정밀도를 낮추지 않고 중단한다.

```python
from camera_platform.sdk import PlatformClient

with PlatformClient("http://127.0.0.1:8780", timeout=120) as client:
    options = client.db_design_options()
    job = client.submit_db_design_study(options["defaults"])
    result = client.wait(job["id"], timeout=3700, poll_interval=2)
    assert result["status"] == "succeeded", result.get("error")
    print(result["result"]["summary"]["result_url"])
```

CLI: `PYTHONPATH=src .venv-platform/bin/python tools/run_db_design_demo.py`.
`--run run-…`으로 기존 작업을 기다릴 수 있다. IMX307은 `--sensor imx307`으로 선택한다.

API: `GET /api/v1/db-design/options`, `POST /api/v1/db-design/studies`.
조회·cancel·파일·bundle은 `/api/v1/reference-workshop/runs/{id}`를 재사용한다.
각 실행은 외장 SSD의 `reference-workshop/{id}/candidates/{candidate-id}`에 저장하며
`sources.json`, `pixel-cell.json`, `capture.json`, `calibration.json`과 전체 해시 명세를 제공한다.
추가 센서의 취득은 새 평균 전자율에서 수행된다. 기존 Reference RAW를 스케일하지 않는다.

## 이전 기하광학 데모 · 2026-10-13 · 특허 F/4.12 조건

실행 `run-33817fe3ce14497d88aa4ef55e0b4cda`에서 네 조합의 RAW 256장과 ISP를 새로 계산했다.
[기존 F/2.75와 F/4.12 비교](http://127.0.0.1:8780/db-design.html?run=run-33817fe3ce14497d88aa4ef55e0b4cda&baseline_run=run-ee2e6236772e4e088c085b3ed0b12d1f&candidate-a=candidate-02&candidate-b=candidate-02&field=center&chart=texture&stage=isp)와
[수정 후 Reference와 비교](http://127.0.0.1:8780/db-design.html?run=run-33817fe3ce14497d88aa4ef55e0b4cda&candidate-a=candidate-00&candidate-b=candidate-02&field=outer-ne&chart=resolution&stage=isp)를 제공한다.

기존 Reference 렌즈의 RAW 128장은 이전과 bit-identical, 수정 p0248의 RAW 128장은 모두
달라졌다. 공통 CCM과 원본 DB·이전 결과는 보존했다. p0248×IMX462 중심 평균 광전자는
1044→467 e⁻로 감소했고 기하광학 MTF는 높아졌다. 회절은 포함하지 않은 모델 내 변화다.
계산·저장 378.62초, 571,330,382 bytes. 1,418개 파일 해시와 native RAW 다운로드를 확인했다.
근거는 `outputs/db-design-fnumber-20261013/verification.json`과 위 수정 문서에 있다.

## 이전 데모 · 2026-10-12 · source-stop 조건

실행 `run-ee2e6236772e4e088c085b3ed0b12d1f`는 두 렌즈와 IMX462/IMX662의 네 조합을
완료했다. [센서 변경 비교](http://127.0.0.1:8780/db-design.html?run=run-ee2e6236772e4e088c085b3ed0b12d1f&candidate-a=candidate-00&candidate-b=candidate-01&field=outer-ne&chart=color&stage=isp),
[렌즈 변경 비교](http://127.0.0.1:8780/db-design.html?run=run-ee2e6236772e4e088c085b3ed0b12d1f&candidate-a=candidate-00&candidate-b=candidate-02&field=outer-ne&chart=resolution&stage=isp)에서 연다.

- 정수 RAW 256장, 각 256×256. 패키지 570,131,573 bytes.
- 광학 캐시가 준비된 상태의 계산·저장 130.17초, 제출부터 완료까지 약 156초.
  최초 렌즈 추적 비용은 이 시간에 포함되지 않는다.
- 파일 명세 1,418개 해시 및 실행 전후 source snapshot/계산 코드 동일성 확인.
- IMX662만 변경한 outer-ne 예: 입력 환산 시간 잡음 25.09→24.03 e⁻ RMS,
  공통 CCM 색차 ΔE00 7.99→8.55. 잡음 감소와 색 손실을 함께 보여주는 모델 내 비교다.
- 새 렌즈는 특허 표 F/4.12와 계산에 사용한 source-stop F/2.751을 구분해야 한다.
- IMX307은 선택·실행 adapter를 제공하지만 이 준비 결과의 촬영 후보에는 포함하지 않았다.

검증 기록은 `outputs/db-design-delivery-20261012/verification.json`, 재실행 명세와
전체 결과는 같은 디렉터리의 `submitted.json`과 `delivery.json`에 있다.

## 조리개와 회절을 함께 비교하는 준비 recipe

화면의 **F/2.75 ↔ F/4.12 · 회절 포함 계산**은 같은 p0248 처방과 IMX462 응답을
고정한 별도 명세다. p0014는 공통 CCM을 만드는 기준 후보로 보존하고, 기본 A/B는
`candidate-01`(source-stop)과 `candidate-02`(특허 F/4.12)다. 두 후보 모두
`exit_pupil_diffraction`을 사용하며 중심·outer-ne의 450–650 nm/10 nm PSF를
계산한 뒤 같은 4 ms, gain 1, 독립 8회 취득과 ISP를 실행한다.

화면은 각 후보의 550 nm 기하·수차 포함 회절·동일 동공 OPD=0 전달함수를 보여준다.
이것은 수신 픽셀을 적분한 native sampled PSF의 이산 변환이며, 연속 렌즈 단독 MTF나
Nyquist까지의 입력 정확도 검증을 뜻하지 않는다. 처리 후 SFR과도 구분한다. 광량은
동일 stop의 역방향 광량 적분에서 한 번만 적용한다. 조건부 PSF를 정규화해도 센서
광량 손실은 없어지지 않는다. [계산 모델과 수렴 조건](camera-platform-aperture-diffraction.md)을
참조한다. 과거 기하광학 결과에는 회절 제외 표시가 유지된다.

```python
with PlatformClient("http://127.0.0.1:8780", timeout=120) as client:
    recipe = client.db_design_options()["recipes"]["aperture_diffraction"]
    job = client.submit_db_design_study(recipe)
    result = client.wait(job["id"], timeout=3700, poll_interval=2)
    assert result["status"] == "succeeded", result.get("error")
```

CLI: `PYTHONPATH=src .venv-platform/bin/python tools/run_db_design_demo.py --comparison aperture_diffraction`.
독립적인 광학 캐시는 재사용하지만 노출별 RAW는 새로 취득한다. 기존 RAW 및
기하 PSF에 Airy 블러를 덧붙여 회절 결과로 표시하지 않는다.

## 현재 데모 · 2026-10-17 · 수차·회절·광량 동시 비교

[F/2.75 ↔ F/4.12 회절 포함 RAW·ISP 비교](http://127.0.0.1:8780/db-design.html?run=run-a3c36ff4c9d24b39966a929836e107ae&candidate-a=candidate-01&candidate-b=candidate-02&field=outer-ne&chart=resolution&stage=isp)를 열면
같은 p0248 처방과 IMX462 구조 응답의 두 조리개를 비교한다. D65,
1000 cd/m², 4 ms, gain 1, 공통 CCM·고정 표시 범위·Adaptive Laplacian을 사용했다.
LSC·NR·sharpen·FPN은 양쪽 모두 OFF다. 독립 취득 8회의 평균 응답과 시간 잡음을 보존했다.

| 시야 | 평균 광전자, F/2.75 → F/4.12 | 처리 후 SFR50, cycles/native pixel |
| --- | --- | --- |
| 중심 | 1044.00 → 467.22 e⁻ | 0.11934 → 0.21101 |
| outer-ne | 853.06 → 442.25 e⁻ | 0.04801 → 0.15828 |

이 모델에서는 조리개 축소의 선예도 이득이 회절을 포함해도 남으며, 평균 광전자는
중심 55.25%, 주변부 48.16% 감소한다. 낮아진 절대 잡음 RMS를 SNR 개선으로 해석하지 않는다.
같은 동공에서 OPD만 0으로 만든 진단은 두 시야·21개 파장 모두에서 작은 조리개의
회절 손실을 보여준다. 중심 550 nm, 0.25 cycles/native pixel에서 이 전달값은
0.86174 → 0.76855다. 실제 수차 포함 값은 0.67581 → 0.68413이며,
기하광학만 계산한 0.62736 → 0.95105의 개선 폭과 구분한다.

두 조리개 RAW 128장과 기준 후보 RAW 64장, 총 192장의 native 256×256 정수 RAW를
새로 생성했다. 패키지는 465,439,186 bytes, 파일 명세 1,232개를 해시 검증했다.
84개 시야·파장·조리개 PSF 노드 모두 수렴 조건을 통과했다. 최대 kernel L1 차이
0.01616, 최소 포착 에너지 0.99732, 최대 독립 OPD 보간 오차 0.01811 waves다.
관련 테스트 44개와 브라우저 회귀를 통과했고, 실제 화면·원본 다운로드를 확인했다.

광학 캐시 준비 후 RAW·ISP 계산/저장은 145.70초였다. 최초 광학 계산은 별도로
기준 159.22초, F/2.75 630.43초, F/4.12 569.16초였으며 두 조리개는 병렬 실행했다.
원본 DB CSV, 과거 두 기하광학 결과의 해시는 유지됐다. 새 진단용 기하 PSF·절대 광량·
균일장 평균 응답도 각 조리개의 과거 값과 일치한다.

실행·검증 자료: `outputs/db-design-diffraction-20261017/{delivery,verification,browser-proof}.json`.
원본 패키지: `/Volumes/T7/CameraE2E/reference-workshop/run-a3c36ff4c9d24b39966a929836e107ae`.
이는 국소 native 탐침의 스칼라 모델 결과이며 연속 렌즈 MTF, Nyquist까지의 일반 장면
정확도, 실측 카메라 성능을 검증한 결과는 아니다.

## 조리개 단계별 선택

`aperture_sweep` recipe는 같은 처방·센서에서 F/2.75–11 범위의 2–8개 오름차순
단계를 받는다. 기본 단계는 2.75, 3.2, 4.12, 5.6, 8, 11이다. 화면의 다음 계산
입력에서 단계를 바꿀 수 있으며, 편집만으로 기존 계산 결과가 바뀌지 않는다.
중심·주변 SFR50, 평균 광전자, 반복 취득 SNR을 표시하고, SNR·상대 신호·포화
제약을 만족하는 후보 중 더 불리한 시야의 SFR50이 가장 높은 후보를 선택한다.
보존된 지표로 선택 조건을 바꾸거나 원본 실행에 연결된 선택 명세를 저장할 수 있다.

[계산 조건·지표·선택 규칙 및 SDK](camera-platform-aperture-sweep.md).
CLI: `PYTHONPATH=src .venv-platform/bin/python tools/run_db_design_demo.py --comparison aperture_sweep`.

## 단계별 조리개 선택 데모 — 2026-10-17

F/2.75·3.2·4.12·5.6·8·11의 실제 수차·회절·광량·RAW·ISP 결과가 준비됐다.
[조리개 비교 화면](http://127.0.0.1:8780/db-design.html?run=run-9692f54b37ab4c5d80f4e9edbd4e83dd&candidate-a=candidate-01&candidate-b=candidate-03&field=outer-ne&chart=resolution&stage=isp)에서
중심·주변 SFR50, Green 광전자, 시간 SNR을 비교하고 조건을 변경해 선택 명세를 저장한다.
기본 SNR 25 dB 조건은 F/4.12, 23 dB 조건은 F/5.6을 선택했다.
고정 조도·노출·센서·ISP 범위의 조건부 결과다.
수치·재실행 방법·검증과 비용은 [조리개 탐색 설명](camera-platform-aperture-sweep.md)을 따른다.

