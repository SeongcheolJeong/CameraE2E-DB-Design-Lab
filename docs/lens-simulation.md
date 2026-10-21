# 렌즈 처방과 PSF의 검증 계약

2026-09-06부터 새 PSF 경로는 처방·계산 상태·LUT의 계약을 검사한다. 기존 데이터는 보존하며 `allow_legacy=True`로만 명시적으로 재현한다. 검사 통과는 특허 처방의 양산 제품 정확도나 실측 화질 인증을 의미하지 않는다.

## 사용

```python
import numpy as np
from pyisetcam import (
    lens_patent_search, lens_patent_raytrace_optics,
    scene_create, scene_set, oi_create, oi_set, oi_compute,
)

rows = lens_patent_search(require_raytrace=True, limit=1)
optics = lens_patent_raytrace_optics(
    rows[0]["simulation_id"],
    allow_approximate_radiometry=True,
)
scene = scene_create("uniform ee", 32, np.array([450., 550., 650.]))
scene = scene_set(scene, "distance", np.inf)  # explicitly select the stored optical state
scene = scene_set(scene, "fov", 5.)
oi = oi_compute(oi_set(oi_create("ray trace"), "optics", optics), scene, crop=True)
```

`allow_approximate_radiometry=True`는 RI와 투과율의 unity placeholder를 수락한다는 뜻이다. 새 pupil 통과율과 방향 데이터는 기하학적 표본 통계이며 calibrated irradiance weights가 아니다. 실제 RAW 광량·색·SNR 예측에는 별도의 T(λ), RI, 각도별 irradiance 가중치 검증이 필요하다. 기본 `lens_patent_optics()`는 `fidelity='diffraction_limited_proxy'`인 이상적 렌즈이며 처방 수차를 적용하지 않는다.

`require_camerae2e=True`는 처방과 package 추천을 확인한다. `require_raytrace=True`는 현재 계약의 PSF 존재도 확인한다. 변환한 처방의 hash가 현재 DB와 다르면 로더가 거부하므로 수정 후 새 PSF를 생성해야 한다. DB 경로를 지정하면 그 DB와 일치하는 파일을 선택한다. 유효한 PSF가 없는 구성은 자동 proxy fallback을 하지 않는다.

## 저장 정보

- SQLite `prescription_validation`: 구성별 오류/가정, 검증 버전, 의미 기반 처방 SHA-256. Infinity 평면, 0 간격, 미상 값, 미해결 변수를 구별한다.
- `reviewed_prescriptions/`: 원문 configuration 표에서 명확히 복구한 값만 적용한 CSV overlay. 원래 export와 raw text는 유지하고 table/row/hash를 기록한다.
- `raytrace_psf_v2/`: `contract_version=2`, 고정 기준 파장, 명시적 물체 거리, 실제 계산 EFL/pupil, 광선 차폐, 비구면 profile, source/code hashes와 PSF 배열.
- PSF: `[row,col,field,wavelength]`, +x 기준 radial field, row0가 +y 방향, pixel-center 좌표. 파장별 PSF는 합=1이며 별도 retained-energy 통계를 둔다.
- geometry/RI/PSF는 독립 field grid와 ideal/distorted height 정의를 가진다. 범위 밖 파장·거리 요청은 거부하고, 지원 image circle 밖은 0이다.
- 추가 배열: image direction cosines, chief direction/CRA, pupil coordinates/conditional weights, admitted pupil fraction. 차폐된 광선은 방향 NaN·가중치 0으로 표현한다.

기하 PSF에 회절이 포함되어 있지 않다. 고해상도 배열 크기는 센서 대역폭의 검증 등급이 아니다. `sampling_validated_for_sensor`와 pupil/spectral convergence는 별도로 표시하며, convergence가 없는 결과를 MTF 인증으로 해석하면 안 된다.

## 재생성·감사

RayOptics를 설치한 Python 환경에서 실행한다.

```bash
python tools/repair_lens_patent_configurations.py \
  --db /path/to/lens.sqlite --export-root /path/to/prescriptions_expanded_v9 \
  --output reports/gap_repair.json --apply
python tools/validate_lens_patent_db.py \
  --db /path/to/lens.sqlite --output reports/prescriptions.json --write-db
python tools/build_lens_patent_raytrace_psf_grid.py \
  --camerae2e-db /path/to/lens.sqlite --export-root /path/to/prescriptions_expanded_v9 \
  --simulation-id p0014:intermediate --preset production \
  --out-dir /path/to/raytrace_psf_v2
python tools/validate_lens_raytrace_assets.py \
  --db /path/to/lens.sqlite --output reports/lens_assets.json --write-db
```

수리 도구는 앞뒤 면 번호와 `dN`·configuration이 유일하게 일치하는 stop gap만 복원한다. 원문 값이 없거나 중복·모호하면 수정하지 않는다. `--apply` 없는 실행은 계획만 출력한다. 구조 검증만으로 기존 추천을 새로 승격하지 않는다.

새 생성기는 입력 불완전, nominal/계산 EFL 불일치, 미상 physical aperture/stop, 부족한 통과광선, PSF support 밖 에너지 유실을 실패로 기록한다. 기본 파장 기준은 550 nm이며 요청 파장의 추가·순서 변경으로 lens state가 변하지 않는다. 근거리 이름의 구성에는 실제 `--object-distance-m`를 지정해야 한다.

기존 시뮬레이션 결과를 확인할 때만 다음과 같이 legacy 경로를 명시한다.

```python
old = lens_patent_raytrace_optics(
    "p0014:intermediate", psf_dir="/path/to/raytrace_psf_highres",
    allow_legacy=True,
)
assert old["fidelity"] == "legacy_geometric_unverified"
```

Legacy 모드는 기존 MATLAB/ISETCam parity를 유지하기 위한 경로다. 새 물리 계약의 검증 결과로 취급하지 않는다.
