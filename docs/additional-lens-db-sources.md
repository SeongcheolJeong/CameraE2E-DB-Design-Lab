# 추가 렌즈 설계 DB 출처

확인일: 2026-09-06. 목적은 CameraE2E의 공개 광학 처방을 늘리는 것이다.
파일 수, 특허 문서 수, 독립 설계 수, 초점·줌 구성 수를 구분한다.
이번 실제 확보·등록 결과는 [확대 배치 보고서](../reports/lens_source_expansion_v2/README.md)에 있다.

## 우선순위와 확보 경로

| 우선순위 | 출처 | 얻을 수 있는 자료 | 활용과 다음 단계 |
|---|---|---|---|
| 1 | [lens-designs.com](https://www.lens-designs.com/photographic-primes), [줌 목록](https://www.lens-designs.com/photographic-zooms) | 사진·모바일 광학계 ZMX와 일부 LEN/ZAR, 공개 Google Drive 및 Sheets 메타데이터 | 이번 배치에서 모바일·단초점·줌을 확대했다. 미지원 Zemax 명령, 다중 구성, 재질을 해석하면 기존 다운로드의 활용률도 높일 수 있다. |
| 1 | [Photons to Photos Optical Bench](https://www.photonstophotos.net/GeneralTopics/Lenses/OpticalBench/OpticalBenchHub.htm) | 공개 색인 1,280개 항목. 특허 기반 면 처방·비구면 계수·초점/줌별 거리 표 | 이번에는 제조사 범위를 확대했다. text 원본이 구조화돼 추가 대량 수집·정규화에 유리하다. title에서 추출한 브랜드는 특허 권리자와 동일하다고 단정하지 않는다. |
| 2 | [DeepLens lens collection](https://github.com/vccimaging/DeepLens/tree/main/datasets/lenses) | 이번 고정 commit에서 JSON 34개와 ZMX 2개. 카메라·휴대폰·단렌즈·DOE·pancake 등 | 36개 원본 확보. JSON 카메라 처방 변환과 재질·비구면 식의 회귀 검증에 유용하다. 빈 JSON 1개도 실제 원본 상태로 보존했다. |
| 2 | [RayOptics](https://github.com/mjhoptics/ray-optics) | 이번 고정 commit에서 SEQ 29개, ZMX 15개, ROA 26개 | 70개 원본 확보. 형식 변환 검증과 기준 광학계에 유용하다. 테스트 변형·중복·비카메라 광학계가 있어 70개 독립 카메라 설계로 세지 않는다. |
| 3 | [EPO European Publication Server](https://www.epo.org/en/searching-for-patents/technical/publication-server) | EP 공개 특허의 공식 XML·PDF, 일부 문서의 기계 판독 표 | 무인증 공식 다운로드 경로로 실제 문서를 확보했다. XML 표와 수식 원문을 먼저 보존하고 면·거리·재질·비구면 계수를 대조한 뒤 처방을 만든다. |
| 3 | [J-PlatPat](https://www.jpo.go.jp/e/support/j_platpat/patent_search.html) | 일본 공개·등록 특허 검색과 문서 | 일본 광학 업체의 원문 탐색에 유용하다. 공개 웹 검색과 API 대량 수집 권한을 구분한다. 이번 작업에는 후보 검색 큐와 공식 접근 조건을 남겼다. |
| 3 | [KIPRIS Plus](https://plus.kipris.or.kr/eng/main/contents.do?menuNo=300026) | 한국 특허·실용신안 데이터 서비스 | 한국 업체와 KR 문서 보완 후보. 필요한 데이터 상품의 신청·권한·제공 형식을 확인한 뒤 연동한다. 이번에 계정이나 유료 서비스를 신청하지는 않았다. |
| 4 | [OSLO system libraries](https://lambdares.com/support-posts/oslo-catalogs-and-libraries) | 공식 안내상 시스템 처방 800개 이상. Cox·Smith/Genesee·Betensky zoom 라이브러리 등 | 광학계 다양성에는 가치가 크다. 소프트웨어 포함 자료이므로 실제 보유 라이브러리와 개별 사용 조건을 확인해야 한다. 공개 자유 다운로드 DB로 확보했다고 표현하지 않는다. |
| 5 | [Edmund Optics Zemax catalog](https://www.edmundoptics.com/products/services/zemax-catalog/) | 공식 안내상 6,400개 이상 stock optical prescription | 완성 카메라 광학계보다 단렌즈·접합렌즈 등 부품 조립과 대체 부품 탐색에 적합하다. 별도 stock-component 분류로 도입하는 편이 좋다. |
| 5 | [StockLensDatabaseMCP](https://github.com/SynapseOptics/StockLensDatabaseMCP) | 공개 README상 약 7,600개 Edmund·Thorlabs·Ross 부품. SQLite 및 `.lhlt`, 여러 설계 형식으로 export | 별도 배포물에 카탈로그가 포함된다. 부품 DB로 유용하지만 제조사 원문 및 데이터 이용 조건 확인이 필요하다. 이번에는 설치·수집하지 않았다. |
| 보완 | [RefractiveIndex.INFO database](https://github.com/polyanskiy/refractiveindex.info-database) | 재질의 파장별 굴절률·소광계수 및 분산식 YAML | 렌즈 처방 DB 자체는 아니다. 재질 누락을 보완하는 후보이며, 정확한 등급·상태·측정 범위가 맞는 자료만 연결한다. 일반 플라스틱 이름을 특정 광학 등급으로 대체하지 않는다. |

## 수집·변환 시 중요한 구분

**중복:** lens-designs는 P2P에서 가져온 모델을 포함한다고 명시하고, DeepLens도 두
출처를 참고했다고 밝힌다. 원본 SHA와 같은 형식의 정확한 처방 일치부터 검사한다.
공개번호·실시예 일치는 검토 후보이고, 국가가 다른 family 문서는 추가 독립 설계의
증거가 아니다. 같은 렌즈의 JSON·ZMX·SEQ 변환본도 여러 설계로 합산하지 않는다.

**해석:** ZMX의 `DIAM`은 반경, P2P의 구경 열은 직경이다. DeepLens는 버전별로
`ai`의 시작 차수가 달라질 수 있어 명시된 `use_ai2`와 원본 코드를 대조해야 한다.
좌표 변환·반사·회절·미해결 줌 설정은 일반 순차 굴절 광학계로 조용히 단순화하지 않는다.

**자료 권리:** lens-designs의 [기본 MIT 안내](https://www.lens-designs.com/note-on-ip)보다
개별 파일의 예외 고지가 우선한다. P2P는 공개 접근과 재배포 허가를 구분한다.
DeepLens Apache-2.0, RayOptics BSD-3-Clause, StockLensDatabaseMCP MIT는 저장소의
라이선스이며 그 안에 인용된 모든 제3자 광학 자료를 일괄 재허가한다는 뜻은 아니다.
manifest에 파일별 고지와 원문 근거를 보존한다.

**특허 접근:** [EPO OPS](https://www.epo.org/en/searching-for-patents/data/web-services)는
등록 기반 데이터 서비스이고, Publication Server의 공개 문서 다운로드는 별도 경로다.
J-PlatPat/KIPRIS도 웹 검색 가능 여부를 인증 없는 bulk API 가능 여부로 바꾸어 해석하지 않는다.
EPO 구형 XML의 변환 문제에 대비해 PDF를 함께 보존한다.

## CameraE2E에 남기는 결과

- 원본과 출처: `camerae2e_db/sources/<source>/manifest.json`, 파일별 URL·SHA256·권리 근거.
- 해석 가능한 처방: `camerae2e_db/catalog.sqlite`의 `public_optical_prescription` revision.
- 해석 불가 원본: 이유와 불변 원본 artifact를 보존하며 완성 렌즈를 만들어 넣지 않는다.
- 특허 문서: 공식 원문 및 표 추출 단계로 관리한다. 정규화된 렌즈 처방 수에 포함하지 않는다.
- 품질: 파서/구조 통과, 제한된 광선 추적, PSF/MTF 및 실측 검증은 서로 다른 단계다.

지금은 공개 처방 수집과 JSON 변환을 확장하고, 이후에는 **이미 받은 ZMX/SEQ의
지원 범위 확대 → 공식 EP/JP/KR 표 대조 → 필요 시 stock 부품 DB** 순서가 효율적이다.
