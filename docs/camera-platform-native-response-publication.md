# 각도별 Sensor 응답 번들의 연구용 등록

`camera_native_response_bundle_v1`의 원본 파일, 절대 4사이트 수집 EQE,
작성자가 제출한 qualification을 SHA-256으로 고정하고 기존 Factory Lens의
PSF·결합 입사 방향에 대조합니다. 결과는 별도의 Sensor 자산과 draft 모델입니다.
원본 qualification의 `passed`는 작성자 보고이며 독립 실측 또는 제품 검증이
아닙니다. 등록 결과의 `physical_validation`은 항상 `not_established`,
`product_ranking_allowed`와 `commercial_release_allowed`는 `false`입니다.

모델을 만들려면 같은 `component_id`·`revision_id`를 자산 속성에 선언한
별도의 Sensor readout 모델을 지정해야 합니다. 그 모델에서 full raster geometry,
RGGB readout과 operating domain을 가져옵니다. 응답과 readout의 같은 ID 선언은
연결 조건일 뿐 동일 실물의 독립 검증은 아닙니다. 일치하는 ID가 없거나 pitch,
RAW 형식, Lens 파장·PSF sampling·입사 방향 범위가 다르면 거부합니다. 기존
평균 QE·상대 각도 LUT를 4사이트 응답으로 추정하거나 이미 포함된 CFA·microlens·
흡수·수집효율을 다시 곱하지 않습니다. Generation-only 응답은 별도 charge
collection 모델이 없으면 등록하지 않습니다.

Python 진입점은 다음과 같습니다.

```python
from camera_platform.native_response_publication import (
    extract_native_response_bundle,
    publish_native_response_bundle,
)

bundle_dir = extract_native_response_bundle(upload_zip_path, empty_temporary_dir)
published = publish_native_response_bundle(
    service,
    bundle_dir,
    expected_manifest_sha256=reviewed_manifest_sha256,
    lens_model_id=qualified_psf_factory_lens_id,
    readout_model_id=same_declared_revision_readout_id,
    name="Reviewed research Sensor",
    revision="1",
    license="Explicit source rights statement",
)
```

API는 사용자가 입력한 서버 파일 경로를 열지 않고 multipart ZIP을 임시 폴더에
추출해 같은 함수를 호출합니다. API 업로드는 압축 파일 64 MiB, 압축 해제 파일
합계 128 MiB로 제한합니다. Python의 직접 ZIP 추출 helper는 각각 512 MB이며,
member 수와 압축비, traversal·symlink·중복 경로·미기재 파일도 제한합니다.
원본 파일은 내용 주소 blob으로 보존합니다. 같은 bytes를 여러 원본 경로가
사용해도 각 경로·역할·SHA는 남고 하나의 typed Sensor asset 소유자로 조회합니다.

등록 Sensor 모델은 qualification에 사용한 **그 Lens 모델 ID**와 고정됩니다.
다른 Lens와 계산하려면 새 응답 적용 검토와 모델 revision이 필요합니다.
제출·Worker에서 원본 artifact 소유자와 bytes, qualification, Lens Factory 근거,
readout 모델 snapshot을 다시 확인합니다. ZIP 추출 및 등록 성공은 파일 정합과
명시된 계산 계약의 증거이며 모델 오차, 공간 crosstalk, MTF 또는 HDR/LFM
제품 성능의 증거가 아닙니다.
