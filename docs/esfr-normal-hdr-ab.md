# E-SFR 일반 / DCG HDR 독립 합성 A/B 실행

두 모드는 같은 2560×1440 차트, 카메라 색 혼합, 광학 blur, 조명, 노출 10 ms, gain 1, CFA/ADC black·white 수준을 사용한다. DB 출처는 run-08c20c6eb15d486f889382f516ad4966 / candidate-03 / condition-006이다. 이 출처는 조건부 보정 후보와 재실행의 연결이며 실제 제품 DCG 검증을 의미하지 않는다.

일반 모드는 단일 low-gain readout이다. HDR 모드는 한 노출에서 생성한 **같은 photoelectron 배열**을 high/low gain 두 readout에 전달한다. High gain은 낮은 read noise, low gain은 높은 포화 용량을 제공하고 고정 3800–4500e− 전이 구간에서 결합한다. 각 프레임의 photon/read noise seed는 독립이다. 입력의 공개 합성 가정은 full well 20000e−, low-gain read noise 12e−, high-gain read noise 2e−, high-gain saturation 5000e−다. 이 값은 측정값이 아니다.

RAW는 같은 12bit full-scale 상대 선형 신호로 내보낸다. 일반 ISP는 고정 bilinear, HDR ISP는 bilinear 후 휘도 Y에 대해 `RGB / (0.65 + 0.35Y)`를 적용하는 고정 전역 Tone 응답이다. 프레임별 white·brightness 재정규화는 하지 않는다. 두 TIFF는 linear sRGB primaries로 선언하며, HDR 파일의 Tone 응답과 색 공간·transfer encoding은 구분한다. PQ/HLG·다중 노출 복원·motion·flare·AE는 포함하지 않는다.

각 모드에서 개발 3쌍으로 유효 MTF·WB·노출 스케일·CCM과 Tone 전방 응답을 고정하고, 취득 계약 발행 뒤 새 검증 3쌍을 생성한다. 총 12개 RAW/ISP 원본의 ID·바이트·픽셀 hash가 모드·역할 사이에서 겹치지 않아야 한다. 숨겨진 색 혼합·blur 정답은 피터에 전달하지 않는다. 노이즈 반복 수 3은 보고서에 표시되며 표본 표준편차는 신뢰구간이 아니다.

기존 RAW SFR RMSE 0.03 / matched ISP 0.05 기준은 유지한다. HDR 실제 ISP는 고정 Tone을 포함하므로 명시적으로 diagnostic으로 선언한다. 실제 ISP와 Reference ISP의 일치성을 임의로 가정해 합격시키지 않는다. 합성 입력도 `comparison_mode: diagnostic`을 명시하면 이 범위를 지킨다. 기존 미선언 synthetic 빌리니어 경로는 호환된다.

`POST /api/v1/camera-validation/esfr/measured/iq-comparisons`에 `workflow_a_id`, `workflow_b_id`를 지정하면 A/B 보고서를 발행한다. v3 출력에는 18색, 20계조×7채널 Noise, 20계조×RAW/ISP Tone, 112위치×8채널 실제 MTF50의 평균·표본 표준편차·B−A·유효 수·CSV·그림이 포함된다. RAW Gr와 ISP Y에는 고정 후보의 SFR 오차도 별도로 표시한다. MTF50은 원 validation의 exact-owner `esfr-images/full-result.json.gz` 관측에서 읽는다. 시뮬레이션 오차를 실제 선예도로 바꾸어 해석하지 않는다.

보고서에는 독립 검증 3장의 첫 촬영에서 RAW / Reference 전 / Reference 후 / 실제 ISP의 모드별 원 preview를 복사해 8개 이미지를 A/B로 표시한다. 원 파일 SHA를 보존한다. Color·Noise·Tone·A/B 우위 판정은 기준 미선언 상태로 보류한다. 차트 범위의 결과에서 전체 DR stops나 실제 HDR 성능을 추정하지 않는다.

정확한 DB tuple의 최신 A/B는 `GET /api/v1/camera-validation/esfr/measured/iq-comparisons/latest?source_run=…&candidate=…&condition=…`와 기존 `iq-latest`의 선택적 `comparison`으로 조회한다. DB Design Lab 카드는 A/B HTML·ZIP을 연결한다. 원 DB 모델과 활성 color/core/unified 모델은 보존한다.

재현 실행은 고정 릴리스의 `tools/run_esfr_hdr_ab_live.py --base-url http://127.0.0.1:8780 --output <new-folder>`로 수행한다. 기존 receipt를 재사용하며 결과를 덮어쓰지 않는다. 원본·두 workflow·취득 계약·보고서·ZIP manifest와 각 파일 SHA를 검증하고, 완료 근거는 항상 `source_kind: synthetic`, `physical_validation: not_established`다.

## 새 설치에서 재현

과거 DB tuple은 이 문서의 원본 실험 출처입니다. 현재 도구는 과거 ID를 기본값으로 사용하지 않습니다. [독립 예제 설치·재계산](esfr-portable-examples.md)의 `--create-db-source` 또는 현재 서버의 명시적 source를 사용하세요. 클라이언트의 로컬 SQLite 의존성도 제거했습니다.
