# 공개 렌즈 설계 파일 수집과 등록

공개 광학 설계 파일을 `camerae2e_db/catalog.sqlite`의 부품 카탈로그에 추가한다.
수집 원본, 파싱한 구성, 구조 검증, 실제 실행 결과는 각각 보존한다.
기존 특허 v6/v9 SQLite와 기존 PSF 자산은 수입 과정에서 수정하지 않는다.

## 확대 배치 — 2026-09-06

추가 출처의 우선순위·자료 형식·접근 조건은 [추가 DB 출처 정리](additional-lens-db-sources.md)에 있다.
기존 두 출처를 각각 200개로 확대하고 DeepLens 36개, RayOptics 70개를 확보해
공개 설계 원본은 **68 → 506개**, 카탈로그 렌즈 component는 **495 → 877개**가 됐다.
이번 누적 원본 중 **200개 파일의 373개 구성**이 파서와 구조 검증을 통과했다.
이는 독립 양산 렌즈 또는 PSF 실행 가능 렌즈의 개수가 아니다.

EP 공식 특허 11건의 XML/PDF 22개와 원문 표 189개도 별도로 확보했다.
특허 원문은 아직 렌즈 처방으로 정규화하지 않았으며 위 506개와 373개에 포함하지 않는다.

P2P의 설정별 `Aperture Diameter` 적용을 보완해 기존 **5개 파일·13개 구성**의
구경 오류를 수정했다. 이전 revision은 보존하면서 `source_aperture_consistency=failed`와
수정 revision ID를 연결했다. 초기 배치의 검사 수치는 당시 구현의 이력이며,
현재 값은 [확대 결과 및 감사 보고서](../reports/lens_source_expansion_v2/README.md)를 기준으로 한다.

## 첫 수집 배치 — 2026-09-06

| 출처 | 원본 파일 | 내용 |
|---|---:|---|
| [lens-designs.com](https://www.lens-designs.com/) | 36 | 모바일 20, 사진용 단초점 10, 줌 6, ZMX |
| [Photons to Photos](https://www.photonstophotos.net/GeneralTopics/Lenses/OpticalBench/OpticalBenchHub.htm) | 32 | Sigma 10, Samyang 8, Leica 7, Zeiss 7, OpticalBench text |

카탈로그 렌즈 component는 **427 → 495개**로 증가했다. 추가 파일 68개 중
31개 설계의 **78개 구성**이 파서와 구조 검증을 모두 통과했다. 나머지 37개 파일은
불완전한 staging 모델로 보존했다. 전체 115개 snapshot은 P2P의 명시적 79개 구성과
ZMX 36개 snapshot으로 구성된다. 미지원 ZMX multi-configuration의 snapshot은
모든 줌 상태를 해석했다는 의미가 아니다. 추가 면 행 2,713개에는 구성별 반복이 포함된다.

실제 RayOptics 진단은 대표 9개 파일의 첫 구성을 대상으로 했으며, **4개 설계·32개
광선이 통과**, 5개는 차단됐다. 통과 사례는 모바일 US07889441과 Sigma 50mm,
Samyang 50mm, Zeiss 50mm 사례다. 파장 587.5618 nm, 물체측 필드 0°/5°,
중앙 근처 pupil 표본 4개에 대한 구경 검사와 유한 광선 도달 검사다.
모바일 사례의 원본 ZMX/정규화 모델 비교는 132개 형상 항목을 통과했고,
광선 도착 좌표 차이는 0 mm였다. 전체 pupil·전체 화각·PSF·회절·MTF·실측 화질은
이 진단으로 검증되지 않는다.

파일별 상태는 [inventory.csv](../reports/lens_source_expansion/inventory.csv),
검증 및 보존 결과는 [audit.json](../reports/lens_source_expansion/audit.json),
광선 추적 세부사항은 [raytrace_smoke.json](../reports/lens_source_expansion/raytrace_smoke.json)에 있다.

## 재실행

Python 3.12 이상을 사용한다. 이 작업에서 사용한 환경은
`/Users/seongcheoljeong/RayOptics/rayoptics-env/bin/python3.12`이며,
`--resolve-glass`와 광선 진단에는 별도 설치된 opticalglass/RayOptics가 필요하다.

```bash
python tools/acquire_lens_designs.py --mobile 20 --primes 10 --zooms 6
python tools/acquire_p2p_lenses.py --limit 32
python tools/verify_public_lens_raytrace.py
python tools/import_public_lens_sources.py \
  --manifest camerae2e_db/sources/lens_designs/manifest.json \
  --manifest camerae2e_db/sources/photons_to_photos/manifest.json \
  --root camerae2e_db \
  --legacy-db camerae2e_db/lens_db/CameraE2E_Lens_DB_v9_20260627/data/lens_patents/lens_patent_simulation_v9.sqlite \
  --resolve-glass \
  --raytrace-report reports/lens_source_expansion/raytrace_smoke.json \
  --report reports/lens_source_expansion/import.json
```

두 수집기에 `--offline`을 전달하면 이미 확보한 원본을 네트워크 없이 검사한다.
현재 lens-designs의 `--total 200`, P2P의 `--limit 200`은 기존 파일을 유지하는
누적 목표다. 전자는 모바일 우선·단초점/줌 균형, 후자는 브랜드 분포를 넓히는
선정 방식이다. 위 명령은 초기 배치의 예시이며, 현재 506개를 재검증하는 명령은
확대 배치 보고서에 있다. 이번 작업은 전체 사이트 미러가 아니다.

기존 데이터가 있는 root에 재수입하면 같은 원본·같은 구성이 중복 등록되지 않는다.
원본 내용이 변경되면 같은 출처 component 아래 새 revision을 생성한다.
정규화 모델과 원본 바이트는 각각 불변 snapshot, SHA256별 artifact로 보존하며,
수입 시작/끝에 원본과 manifest 해시를 확인한다. DB transaction 실패 시 행은
되돌리지만 이미 생성된 불변 사본이 참조 없이 남을 수 있다.

## 검색

```python
from pyisetcam.component_catalog import component_catalog_search, component_catalog_get

rows = component_catalog_search("Sigma", family="lens", root="camerae2e_db")
detail = component_catalog_get(rows[0]["component_id"], root="camerae2e_db")
print(detail["component"])
print(detail["revisions"])
print(detail["runs"])
```

`public_optical_prescription` revision에는 원본 설정과 정규화 면이 들어 있다.
`public_prescription_structure`는 파싱/구조 검사, `geometric_raytrace_smoke`는
특정 실행의 제한된 광선 검사다. `scientific_accuracy`는 계속 `unknown`이다.
원본 f-number가 없는 경우 임의 값을 채우지 않는다. P2P `Image Height`의 원문은
`image_height_source_mm`에 보존하며, 표준 센서 호환성 필드를 추측으로 채우지 않는다.
등록된 component나 구조 검사 통과를 기존 `lens_patent_*` PSF 실행 ID로 사용할 수는 없다.

## 중복 판정과 보류 사유

- 원본 SHA256 및 같은 포맷 내 처방/광학 설정의 정확한 일치를 검사한다.
  파일명·제목·메모·숫자 표기 차이는 제외하지만 pupil·field·파장·환경·미해석 광학
  제어값은 유지한다. ZMX/P2P 간 동등성을 확인하지 않고 자동 병합하지 않는다.
- 특허 공개번호/실시예 일치는 검토 후보로 기록한다. 이번 68개는 v9와 해당
  공개번호가 겹치지 않았지만, 다른 국가의 동일 patent family까지 확인한 것은 아니다.
- HYPR 등 의미를 확인하지 못한 제어 명령, 좌표 변환/반사, 미해결 줌 설정,
  누락된 재질/Abbe 값, Leica 사례의 원문 비구면 이견은 보류한다.
- named GLAS의 1.5/40 같은 placeholder를 실제 재질로 사용하지 않는다.
  `--resolve-glass`는 원본 GCAT 목록 내에서 유일하게 일치하는 이름만 로컬
  카탈로그로 확인하고 분산 계수·패키지 버전·해시를 보존한다.
- DIAM의 반경과 CLAP의 물리 구경, ZMX EVENASPH PARM1=A2와 P2P 기본 A4를
  구분한다. RayOptics native reader가 첫 번째 면 STOP을 잃는 사례는 별도
  기록하며 정규화 모델은 명시적인 원본 STOP을 사용한다.

## 출처와 이용 근거

각 manifest에는 출처 URL, 수집 시각, SHA256, 원문 메타데이터와 권리 상태가 있다.
lens-designs 파일은 [사이트의 기본 MIT 안내](https://www.lens-designs.com/note-on-ip)를
근거로 기록했고, 개별 파일 안에는 MIT 문구가 없다는 점도 구분했다. 원본 작성자가
재질·직경·구경 등을 보완했을 수 있으므로 양산품 처방과 동일하다고 보증하지 않는다.
P2P는 사이트의 All Rights Reserved 문구를 보존했으며 공개 접근을 포괄적인
재배포 허가로 해석하지 않았다. 이 작업은 로컬 수집·등록이며 외부 배포는 수행하지 않았다.
