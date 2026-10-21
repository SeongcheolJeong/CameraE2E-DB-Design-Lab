# 수집한 TechInsights 자료의 센서 모델 반영

기본 `physical_estimate_v1`은 원문에서 검토한 구조값을 우선 사용한다.
현재 적용 대상은 IMX307, IMX462, IMX662이다. 다른 센서는 기존 canonical
입력을 유지한다. 구조 측정과 제품별 광학 응답 측정은 별도로 취급한다.

| 입력 | 적용 내용 | 원문 근거 |
| --- | --- | --- |
| IMX307 | 피치 2.9 µm, 활성 Si 2.8 µm, 광학 높이 2.4 µm 확인 | DEF-1807-801 p6 |
| IMX462 | 활성 Si 6.2 µm, 광학 높이 2.5 µm, CFA 0.45–0.55 µm 확인 | DEF-2011-804 p7–9 |
| IMX462 그리드 | 개구 면적비 0.88 → 등가 정사각 개구 폭비 √0.88 | DEF-2011-804 p8 |
| IMX662 렌즈 | 높이 1.3 µm, TEM 단면에서 보고한 곡률반경 1.6 µm | DEP-2206-801 p97 |
| IMX662 CFA | 식별된 녹색 필터만 0.50 µm 적용 | DEP-2206-801 p100 |
| IMX662 그리드 | 개구 면적비 0.854 → 등가 정사각 개구 폭비 √0.854 | DEF-2206-805 p9; DEP-2206-801 p117 |

IMX662 DEF와 DEP는 동일 부품·카메라·다이 치수와 동일한 분석 이미지 ID
405562로 연결했다. 단순한 제품명 유사성으로 다른 보고서의 치수를 가져오지 않았다.
모든 적용값에 보고서 코드, 1부터 시작하는 PDF 페이지, 원본 SHA-256을 보존한다.

녹색 이외의 DEP 필터는 0.55 µm이지만 R/B 중 어느 색인지 식별되지 않았다.
따라서 R/B 명목값은 기존 DEF 범위의 중간값 0.475 µm이라는 가정으로 유지한다.
DEF 범위 0.42–0.53 µm와 DEP 범위 0.50–0.55 µm를 모두 보존하며, 합친
0.42–0.55 µm는 민감도 시나리오 범위이지 통계적 신뢰구간이 아니다.

전체 광학 높이 2.6 µm를 유지하면서 렌즈·채널별 CFA를 뺀 나머지는 미분해
유전체층으로 계산한다. 녹색에서는 `2.6 − 1.3 − 0.5 = 0.8 µm`이다.
보고된 버퍼·AR·코팅 치수를 여기에 다시 더하지 않는다. 서로 다른 위치의 치수를
정밀한 평면 적층 측정으로 오인하지 않는다.

## 반영 범위와 남은 한계

- 녹색 CFA 두께는 Beer–Lambert 흡수와 QE 계산에 들어간다. CFA `n,k`는 여전히
  공통 proxy이며, 두께 측정만으로 제품별 투과 스펙트럼을 확보했다고 표시하지 않는다.
- 렌즈 높이·곡률·그리드 개구는 own-site 수용 진단에 들어간다. 원래 픽셀을 벗어난
  빛의 이웃 픽셀 수집은 미해결이므로 명목 QE에 이 진단값을 곱하지 않는다.
- 보고된 gapless 배열이나 한 개 곡률값은 전체 렌즈 표면, 비구면 형상, 공간적
  크로스토크 또는 주변부 색 음영을 검증하지 않는다. 원형 footprint와 광선 묶음은
  여전히 근사이다. 렌즈 높이 1.3 µm는 전체 유기 OCL 두께이며 정확한 cap sag로
  동정한 값은 아니다.
- IMX662 AR1 AlO 13 nm, AR2 TaO 44 nm, BPMD SiO 200 nm와 위치별 다른 치수는
  근거로 보존한다. EDS 원소 조성은 광학 분산 `n(λ),k(λ)` 측정이 아니며,
  현재 비간섭 계산은 AR 박막 간섭을 계산하지 않는다. AR 예측에는 재료 분산과
  coherent 박막/비간섭 두꺼운 층을 결합하는 모델이 추가로 필요하다.
- DTI와 표면 역피라미드로 QE·수집 효율·FWC를 임의 추정하지 않는다.

기존 자동 추출의 CFA 두께→파장 범위 오분류와 주변 다이 실링 DTI→활성 픽셀
DTI 오분류를 수정했다. IMX462 활성 영역은 partial back DTI이고 IMX307 활성
영역 DTI는 이 원문에서 미확인이다. 기존 생성 DB는 원자료 추적을 위해 보존하고,
새 조회의 검토 결과와 모델 provenance에서 정정 사실을 제공한다. 수정된 생성기는
향후 추출에서 해당 오류를 방지하고 PDF 페이지·해시를 보존한다.

## 사용과 재현

```python
from pyisetcam import image_sensor_db_config, sensor_techinsights_evidence

config = image_sensor_db_config("DEF-2206-805")
evidence = sensor_techinsights_evidence("DEF-2206-805")
print(evidence["selected_facts"]["green_cfa_thickness_um"])
print(evidence["active_pixel_dti"])

# 선택적으로 보유한 원본 PDF의 해시를 다시 검사한다.
checked = sensor_techinsights_evidence(
    "DEF-2206-805", source_root="/path/to/Sensor_DB_TechInsight"
)
print(checked["source_checks"])  # matched / unavailable / mismatch

# 이번 근거 반영 전 physical estimate의 수치를 재현한다.
previous = image_sensor_db_config(
    "DEF-2206-805", optical_assumptions={"geometry_source": "canonical_db"}
)
```

`source_root`를 생략하면 원본 검사는 `not_checked`이며, 원본이 없는데 검증됐다고
표시하지 않는다. 패키지에는 검토된 사실만 들어가므로 원본 PDF 없이도 실행할 수 있다.
새 시나리오는 근거 manifest 해시를 저장하며, 실행 시 해시가 바뀌었다면 명시적으로
재구성해야 한다. 원본 DB와 FDTD용 canonical material bundle은 변경하지 않는다.

```bash
python tools/verify_sensor_techinsights.py --source-root /path/to/Sensor_DB_TechInsight
python tools/compare_physical_sensors.py --smoke --no-sensitivity \
  --output reports/techinsights_sensor_e2e/comparison
```

전체 자료 검색의 범위와 한계는 [감사 결과](../reports/techinsights_sensor_e2e/findings.md),
이번 구현과 수치 검증은 [구현 결과](../reports/techinsights_sensor_e2e/implementation.md)를 참조한다.
