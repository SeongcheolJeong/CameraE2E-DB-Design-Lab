# 시간 장면의 소스 위치 상대 조도

`temporal_scene_v1` Run의 `parameters.field_radiometry_mode`를
`"source_position_ri_v1"`로 지정하면, 각 **보존된 분광 영상 knot**에서
`W/m²/nm` 이상상 광입력에 Lens의 파장별 소스 위치 상대 조도를 곱한다. 그 결과를
단위합 pixel-integrated PSF로 수신 위치에 전달하고, Lens directional response와
센서 angular QE를 수신 위치에서 적용하여 `electron/pixel/s` 광전자율을 만든다.
rolling/global 행별 노출 적분과 정수 RAW/ISP는 이 knot별 전자율 이후에 실행된다.

```json
{
  "plugin_id": "temporal_scene_v1",
  "scene_sequence_id": "REGISTERED_TIMED_SCENE_ID",
  "control_sequence_id": "REGISTERED_CONTROL_ID",
  "parameters": {
    "field_radiometry_mode": "source_position_ri_v1",
    "seed": 821,
    "capture_crop": {"crop_origin": [10, 10], "shape": [12, 12]}
  }
}
```

Run은 원본 scene/control/test/assembly/model ID도 요구한다. `capture_crop`은
선택 사항이며, 렌더된 halo source가 이미 receiver ROI를 고정했다면 그 값과
정확히 일치해야 한다. source support의 native `crop_origin`을 RI·field PSF에
사용하고 receiver crop은 광학 전달 뒤에 취한다. 소스와 수신 geometry가
달라져도 Lens RI나 pixel aperture가 두 번 적용되지 않는다.

선언된 knot `i`의 광전자율은 `q_i(r,c)`이다. 프레임 `f`, native 행 `r`의
기록된 노출창 `[a_fr,b_fr]`과 선언된 영상 보간 기저 `B_i(t)`에 대해
`Q_f(r,c) = Σ_i q_i(r,c) ∫[a_fr,b_fr] B_i(t) dt`를 계산한다. `B_i`는
`linear_irradiance` 또는 ZOH 계약을 따른다. 소스 RI/PSF/방향 응답은
이 경로에서 시간에 고정된 선형 광학 응답이다. 따라서 보간은 **이상상 영상**의
보간이며 물체 자세·가림의 연속 3D 렌더가 아니다. 3D render knot는 각 시각에
고정된 scene render 증거를 가지며, 그 사이의 움직임 정확도는 별도로 입증해야 한다.

`source_relative_illumination_map`은 모든 knot가 공유하는 소스 격자의
무차원 지도다. `summary.array_geometry`가 source support 또는 native scene crop을
명시한다. `flux_diagnostics_per_scene[i]`에는 PSF 전
`pre_ri_source_spectral_flux`, `post_ri_source_spectral_flux`(`W/nm`),
PSF 후 수신 crop flux와 바깥쪽 손실 범위가 있다. `relative_illumination_map`은
수신 위치 진단 지도일 뿐 다시 곱하지 않는다. immutable 원본 분광 배열/렌더
아티팩트는 수정하지 않으며, 제출 때 frozen mode·scene source·모델·자원 예산을
검사하고 Worker 시작 때 등록 원본에서 전체 요청을 다시 만들어 비교한다.
출력 `prediction.method_and_version`은 소스 RI 선택을 구분한다.

독립 테스트는 각 소스 셀과 PSF tap을 직접 합산한 뒤 각 rolling 행의 선형
시간 기저 적분, 광전자량, 정수 RAW까지 비교한다. 별도 등록 테스트는 실제
Mitsuba 3D 이동 가림을 시각별로 렌더하고 source RI → rolling RAW/ISP와
원본 아티팩트 불변성을 확인한다. mode 생략의 전자량·RAW·RGB·summary SHA는
이전 출력을 그대로 유지한다.

## 두 조명 성분 · PWM · 잔류 전하 경로

등록된 `retained_charge_v1`에서도 같은 opt-in을 사용할 수 있지만, 범위는
**v4 trapped HCG/LCG capture program + 명시적 PWM + modulated 및
unmodulated 두 SceneSequence**로 제한한다. 두 시퀀스는 한 clock, native 센서,
파장 축, 광학 조건, receiver ROI를 공유한다. 양쪽 분광 이상상 knot의 source RI를
각각 PSF 전에 적용한다. 이후 modulated 성분만 PWM 주기·duty·위상으로
적분하고, unmodulated 성분은 같은 행 노출창에서 독립적으로 적분한다. 두
광전자 생성량을 **하나의 photodiode free/trapped 상태**에 합친 뒤 전달하고,
전달된 같은 전하를 HCG·LCG 두 경로에서 읽는다. RAW 경로는 광학 내부 정답
전하를 ISP/HDR 보정 입력으로 사용하지 않는다.

`parameters.field_radiometry_mode`의 값은 동일하다. `parameters`에는
`unmodulated_scene_sequence_id`, 필요 시 `capture_crop`, `seed`,
`noise_enabled`도 명시한다. 이 mode는 v1/v2/v3 및 v5 photo-HDR retained
프로그램, 단일 조명 성분, 상수 파형, 비공유 단일 readout에서는 거부된다.
SDK의 `submit_retained_charge(..., field_radiometry_mode=...)`도 같은 Run
계약을 전송한다.

출력은 `source_relative_illumination_map`과
`unmodulated_source_relative_illumination_map` 두 무차원 source-support
지도, 각각의 `flux_diagnostics_per_scene` 및
`unmodulated_flux_diagnostics_per_scene`를 보존한다. 각 knot의 ledger는
source RI 전후 `W/nm`, PSF·receiver crop 후 `W/nm`를 구분한다.
`source_position_ri_components`에는 두 지도의 축·native source support·
receiver crop·knot 내용 해시·보존 서브셀 증거가 성분별로 기록된다. 서브셀은
분수 native 위치에서 RI를 적용하고, 수신 개구를 재적용하지 않는다. 등록된
실제 두 3D Light 성분은 같은 Scene·카메라·시각에서 렌더된 한 쌍임을
`dual_3d_light_evidence`로 별도 확인한다. 일반 저작 이미지 두 개는 그러한
3D 광원 검증으로 승격하지 않는다.

선택 모드는 저장 전 component별 RI 가중 배열·지도 크기와 총 output evidence
크기를 기존 1 GiB working/192 MiB evidence 한도에 더해 검사한다. Worker는
frozen mode·두 시퀀스·capture program·원본 아티팩트에서 요청을 다시 만들어
비교한다. 따라서 mode 또는 한 성분의 knot 변경은 전하 계산 전에 거부된다.
mode 생략의 v4 trapped HCG/LCG 광전자·트랩·두 RAW·RGB·summary SHA는
이전 결과와 동일하게 유지한다.

독립 테스트는 두 성분 각 knot에 대해 source RI → PSF 직접 합산 → native
4-site 전자율을 비교하고, 실제 두 3D Light를 렌더한 등록 테스트는 phase별
PWM/ambient 행 적분, 트랩·두 정수 RAW·ISP 출력과 별도 Worker 재현을 확인한다.
이는 선택한 이산 연산의 구현 검증이다. 3D knot 간 연속 가림 오차, 시간
aliasing, 실제 Lens·센서의 RI/PSF/QE 정확도는 알 수 없다. retained v5
photo-HDR의 source RI 계약은 아직 없다. 따라서 `physical_validation`은
`not_established`다.

## 독립 브래킷 HDR/LFM · 공유 전하 DCG 경로

`hdr_bracket_v1`과 `shared_charge_dcg_v1`도 선택형
`parameters.field_radiometry_mode=source_position_ri_v1`을 받는다. SDK의
`submit_hdr` 및 `submit_dcg`에 같은 이름의 선택 인자가 있다. 생략하면 기존
요청·출력 경로를 유지한다. 등록 Test와 capture program, Lens·Sensor·장면·
Control 원본은 고정하며 Worker가 실행 전에 이를 재구성해 검사한다.

HDR에서는 소스 셀/서브셀의 RI를 knot별 PSF 전에 적용한다. 별도 PWM 변조
시퀀스와 비변조 시퀀스를 쓰면 같은 native 소스 격자와 고정 RI 지도, 수신
기하·파장·clock를 요구한다. 변조 성분만 파형을 적용하고 두 성분의 행 노출
적분을 합쳐 전기 RAW 및 ISP/HDR 처리로 보낸다. `hdr_merge=null`에서는
움직이는 작성 장면·PWM 위상을 RAW로 조사할 수 있다. HDR 합성을 켠 경우
정적 장면과 PWM 정수 주기 노출 계약을 유지하며, 서브셀 광량만 달라져도
정적 조건을 거부한다. 소스 지도와 성분별 flux ledger는 계산 출처다.

DCG에서는 같은 소스 RI와 PSF를 거친 광전하를 한 번 취득한 뒤 HCG/LCG
두 변환 경로에서 읽는다. 정적 광학 입력 조건은 소스 서브셀 광량까지 확인한다.
소스 지도와 knot별 광량 진단은 전하 생성의 계산 근거이며, RAW 기반 합성에는
내부 정답 전하를 넣지 않는다. 두 경로 모두 자원 상한과 보존 원본 검사를
유지한다. 이 추가 경로 역시 Lens 방향 분포와 픽셀 각도 응답, PRNU·전하
수집을 완전히 결합한 실측 모델이 아니며 실제 HDR/LFM 또는 DCG 정확도는
검증되지 않았다.
