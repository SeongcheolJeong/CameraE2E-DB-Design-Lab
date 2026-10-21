# Camera Platform dev27: 출처·입력 계약 보강 및 설치본 검증

이 문서는 dev27 시점 기록이다. MTF 기하 셀 차이와 저장 LFM 전 위상 교차 검사를 추가한 현재 개발판은 [dev28 문서](camera-platform-joint-workflows-dev28.md)를 따른다.

현재 개발판은 `0.16.2.dev27`이다. [dev26의 Lens 처방 OPD·LFM 시간 적분·RAW ISP 탐색](camera-platform-joint-workflows-dev26.md)을 유지하며, 검토에서 확인한 두 가지 조용한 의미 변경을 차단했다.

- Lens DB→OPD bridge는 Object 행이 있으면 **선행하는 단 하나의 명시적 무한거리**만 허용한다. 유한·불명 거리 또는 뒤에 나타나는 Object 행을 버리고 무한거리 물체로 계산하지 않는다. Object 행이 아예 없는 `p0014`는 기존 무한거리 연구 가정으로 유지하며, 이것 역시 원본 scene의 실측 물체거리 검증은 아니다. 정확한 특허 원문·CSV·revision 결합과 회절 PSF 수치 수렴 조건은 [dev26 기록](camera-platform-joint-workflows-dev26.md)을 따른다.
- 등록 RAW의 noise/detail ISP 레시피는 tuning flat/texture와 holdout flat/texture **네 취득 모두**에서 selector·readout·기하·정규화 `processing_binding`이 같아야 한다. 서로 다른 digital gain을 같은 목적함수의 비교군으로 받아들이지 않는다. 네 원본의 RAW 바이트·계보 분리도 제출 전에 검사한다. 이름 길이는 뒤에 붙는 Study 단계명을 포함해 160자 이내가 되도록 미리 제한한다. `get_receipt()`는 원본 Study·선택 record의 해시와 저장 근거를 다시 읽지만, 선택 규칙이나 holdout 판정을 독립적으로 재계산하는 기능은 아니다.

[dev27 wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev27-py3-none-any.whl)의 SHA-256은 `5b6c34171fff013920351ff06126fb8c523113bb63255d5a399dd45787c37166`이다. 별도 설치 경로의 **1,040개 파일**을 wheel과 바이트 대조한 뒤, 새 Registry와 child Worker에서 사용자 분광 장면→8×8 Lens×Sensor 정수 RAW, 별도의 D65 RAW→기존 CameraE2E ISP 재처리, 시야 초과 제출 전 차단, 특허 처방의 OPD 실행을 확인했다. [설치본 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev27-installed-proof.json)는 OPD `REFUSED`, 승인된 회절 PSF 없음, 방향 광량 경로 차단을 기록한다. 이 개발 wheel은 기존 8770 기본 서버를 교체하지 않았다.

같은 wheel에서 [한 위상의 결합 LFM 시간 적분 교차 검사](../reports/camera_platform/lfm-temporal-parity-20260928/dev27-installed-pinned-phase-parity.json)가 선언한 분해능에서 통과했다. 결합식과 별도 정수 ns 적분의 상대 차이는 `4.31214e-10`이며, `saved_lfm_artifact_compared=false`다. 제품 line time·phase·die crop 검증을 의미하지 않는다. 이 wheel의 격리 설치 경로에서 등록 RAW ISP 레시피 테스트 **4개**가 통과했고, 실제 Worker 실행·출처 분리·holdout·원본 RAW 동일성·근거 bundle 재조회 및 서로 다른 readout 거부를 포함한다. 테스트는 source tree가 아닌 wheel 설치 모듈을 import하도록 별도 pytest 설정과 `PYTHONPATH`를 사용했다. 수정된 Lens bridge/PSF 테스트 **33개**와 Ruff도 통과했다.

한계는 그대로다. 저장 렌즈 PSF convolution은 기하 raytrace이며, 특허 처방 OPD의 현재 진단 격자는 PSF 수렴 기준을 충족하지 못했다. MTF는 동일한 550 nm 기하학적 각도 영역까지 맞췄지만 픽셀 적분을 포함한 유효 영역·분광 광량·CRA×픽셀 응답은 맞지 않았다. HDR coverage는 `inconclusive`이고, 합성 mAP는 독립 정답 없는 연결 진단이다. ISP 레시피의 holdout은 출처·계보가 분리됐지만 선택 과정에 대해 blind는 아니며, 현재 Python service API에만 있고 HTTP/SDK·화면에는 연결되지 않았다. 모든 결과의 `physical_validation=not_established`, `product_ranking_allowed=false`를 유지한다.
