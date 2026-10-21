# Lens DB Factory 쌍의 명시적 draft 등록

`lens_db_factory_workflow.plan()`과 `submit()`은 검토한 렌즈 DB revision을 두 독립적인 Factory 입력으로 고정한다. 두 작업의 수치 결과가 `PROPOSED`로 끝나도 Lens 모델은 자동 등록되지 않는다. 등록할 때 사용할 조건, 모델 이름, revision은 시스템 엔지니어가 결과를 검토한 뒤 명시한다.

`lens_db_factory_publication.publish_pair(service, payload)`의 입력은 다음과 같다.
같은 계약은 `POST /api/v1/lens-db-factory-pairs/models`와 SDK의
`client.publish_lens_db_factory_pair(payload)`에서도 제공한다. 먼저
`client.wait(job_id, expected_kind="characterization")`로 두 작업의 완료를
확인하고, 각각 `GET /api/v1/factory/jobs/{job_id}`에서 실제 수치 판정을
읽은 뒤 등록한다.

```json
{
  "specification": {"...": "plan과 submit에 사용한 원본 명세 전체"},
  "expected_plan_sha256": "plan_sha256",
  "asset_id": "submit이 반환한 source asset ID",
  "job_ids": {"radiometry": "run-...", "psf": "run-..."},
  "conditions": {"f_number": {"value": 2.0, "unit": "1"}},
  "radiometry_name": "렌즈 방사·입사각 연구 모델",
  "radiometry_revision": "1",
  "psf_name": "렌즈 PSF 결합 연구 모델",
  "psf_revision": "1",
  "condition_scope": "optical_response"
}
```

위 `f_number` 값은 예시다. 실제 값은 저장된 PSF Job의 `result.summary.geometric_reference_f_number`와 정확히 같아야 한다. `optical_response` 범위에서 노출·gain을 자유롭게 하려면 상위 방사 모델 조건에도 `exposure_s`와 `gain`을 넣지 않는다. 코드가 선언하지 않은 물리적 적용 범위를 넓히지 않도록 두 조건이 있으면 이 범위를 거부한다.

등록 함수는 현재 DB revision과 reviewed CSV를 다시 계획하고, plan hash, source asset과 원본 아티팩트, 두 Job의 asset snapshot·정규화 요청·원본 입력·계산 결과·수치 상태, 독립 표본화의 동일 처방 결합을 확인한다. 이어서 부모 방사 모델과 자식 PSF 합성 모델의 payload·조건 계약을 **첫 모델 저장 전에** 검증한다. 성공하면 같은 요청으로 재시도해도 정확히 일치하는 두 draft 모델을 재사용한다. 자식 저장만 실패하면 `PartialPairPublicationError.parent_model_id`를 반환하며, 동일한 입력을 재시도하면 기존 부모 draft를 재사용한다.

반환되는 `approval_status`는 `draft`, `physical_validation`은 `not_established`다. `product_ranking_allowed`와 `commercial_release_allowed`는 모두 `false`다. 저장된 모델은 출처가 연결된 연구용 계산 입력이지, 실측 렌즈 성능·CRA/픽셀 결합·제품 순위를 검증한 결과가 아니다.

작업은 실행 당시의 Python 소스·라이브러리 환경 지문을 보존한다. 실행 후
소스나 패키지 버전이 바뀌면 이전 작업을 새 환경의 모델로 등록할 수 없다.
변경한 환경에서는 동일한 DB revision과 명세로 Factory 작업을 다시 실행해야 한다.

[dev32 설치본 검증](../reports/camera_platform/integrated-workflows-20260928/installed-p0014-factory-pair-dev32.json)은
검토된 p0014 revision을 격리 설치 wheel의 별도 Worker에서 다시 계산해 두
`PROPOSED` Job을 얻고, 위 SDK 경로로 두 draft 모델을 등록했다. 동일 요청을
재시도했을 때 모델 ID는 변하지 않았다. 이 검증은 Lens 원본·수치 경로·등록
계약을 확인한 것으로 센서 각도 응답이나 실물 렌즈 정확도를 확인하지 않는다.
