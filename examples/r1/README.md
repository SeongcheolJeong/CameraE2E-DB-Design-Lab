# CameraE2E R1 설계 예제 번들

`manifest.json`은 R1 설계 실험에 사용할 등록 모델 6개와 장면 2개의 ID, revision, 파일 해시를 고정합니다. 광학(Lens·PSF), 픽셀(QE·입사각 응답·RGGB CFA), 전기(readout·ADC)는 각각 독립 ID·revision·해시를 가진 부품입니다. 기존 `r1-stop-clear-v1`, `r1-stop-quarter-v1`, `r1-stop-radial-v1`은 원본 전체 모델을 보존하고, 새 세 모델은 등록된 부품을 조합합니다. UI에서는 이 6개 **등록 조합**만 선택하며 임의 부품 자유 조합은 아직 지원하지 않습니다.

새 조합은 `r1-clear-lowgreen-v1`(녹색 QE 0.65배), `r1-clear-8ke-v1`(full well 8 ke), `r1-clear-lowgreen-8ke-v1`(두 변화 동시)입니다. 이 차이는 합성 예제의 정수 RAW까지 반영됩니다. `r1-flat-v1`은 보존된 평탄 분광 복사휘도를 재사용하고 `r1-checker-v1`은 그 입력에서 만든 2파장 checker 예제입니다. 모두 합성 입력이며 실측 장면이나 실제 센서의 예측 정확도는 주장하지 않습니다.

새 실험은 모델 ID 목록과 장면 ID 하나를 명시합니다. 런타임은 해시가 맞는 번들 파일만 읽고 부품의 파장·pixel pitch·RGGB CFA·전기 readout 계약을 확인합니다. 장면 복사휘도 `W/m²/sr/nm`에 선택 광학의 파장별 축상 `E/L`을 정확히 한 번 곱해 이상면 분광 조도 `W/m²/nm`를 구성합니다. 이후 위치별 렌즈 응답, 픽셀 전하, readout, 정수 RAW는 기존 응답 체인을 사용합니다. 지원 입력은 현재 16×16 native sensor crop, 500·600 nm, 고정된 128×128 full-sensor 좌표와 각 모델의 선언된 광학 조건입니다.

예제는 설치 패키지의 `camera_platform/data/r1`에도 동일한 JSON으로 포함됩니다. 기본 실행은 설치 패키지 복사본을 읽으므로 과거 `reports/`나 `outputs/...-qa/blobs`가 필요하지 않습니다. 외부 번들을 명시하려면 `CAMERAE2E_R1_ASSET_BUNDLE`에 절대 디렉터리 경로를 지정하십시오. 수정된 자산을 등록할 때는 각 파일의 ID·revision 및 manifest SHA-256을 새 버전으로 발행해야 합니다. 이미 제출된 작업은 원래 읽은 manifest·모델·장면 해시에 묶여 있으며, 변경된 번들로 조용히 재실행되지 않습니다.

이 번들의 광학 수치는 과거 성공한 synthetic spatial run에서 추출할 때 원본 Factory 입출력 일치가 확인되었습니다. 픽셀·전기 변형은 명시한 합성 변환이며 측정 센서 처방이 아닙니다. 설치본은 간결한 모델 응답과 원본 run 해시를 보존하지만, 전체 Factory 원본 블롭을 포함하거나 설치 시 다시 광선을 추적하지 않습니다. 따라서 해시 검증은 번들 무결성에 대한 확인이고, 물리적 타당성·상용 하드웨어 등가성에 대한 확인이 아닙니다.

재배포 출처는 `manifest.json`의 `distribution_rights`에 고정했습니다. 기존 세 모델의 원천 Lens asset, 부모 Lens와 Ray/PSF Factory 입력, 평탄 장면의 원천 조명/scene asset이 모두 내부 기록상 MIT로 선언된 작성 합성 fixture입니다. 추가 픽셀·전기 변형도 그 원본에서 작성한 합성 변환으로 기록했습니다. 최종 Lens의 "Mixed parent rights" 문구는 이 부모와 PSF Factory 입력을 가리킵니다. 제품에는 CameraE2E MIT `LICENSE` 고지를 함께 포함해야 합니다. 이 기록은 보존된 입력의 권리 선언 확인이며 별도 외부 권리 심사를 뜻하지 않습니다.
