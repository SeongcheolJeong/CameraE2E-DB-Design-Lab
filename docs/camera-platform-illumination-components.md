# 독립 비변조·PWM 광입력 — 개발 소스 0.16.1.dev22

일반적인 주변광과 LED가 함께 있는 장면에서 PWM을 영상 전체에 곱하면 주변광까지
깜박이는 잘못된 계산이 된다. `hdr_bracket_v1`은 이제 **두 개의 작성된 분광 이상 영상
시퀀스**를 선택적으로 받는다. 기존 `scene_sequence_id`는 파형이 곱해지는 성분이고,
`unmodulated_scene_sequence_id`는 파형을 곱하지 않는 성분이다. 둘을 RAW에서 추정하거나
자동으로 분리하지 않는다. 단일 장면 요청의 배열과 요약은 기존 결과와 동일하다.

두 시퀀스는 각각 Registry의 불변 ID·해시·원본 image artifact를 가진다. 같은 시계,
파장, 단위, 기준면, 포함 효과, native sampling과 image shape가 필요하다. Test의
`electrical_hdr_components_test_v1/v2` stimulus가 두 ID·해시를 고정하고, 실행 요청은
두 번째 ID를 `parameters.unmodulated_scene_sequence_id`로 전달한다. Worker는 등록
원본에서 요청을 다시 구성하고 source/normalized Capture Program artifact까지 검사한다.
시퀀스나 고정 요청이 달라지면 취득 전에 거부한다.

각 수신 행의 광전자 수는 다음의 선형 합이다.

```text
변조 성분 전자 = ∫ 분광 영상의 광전자율(t) × 작성 PWM 파형(t) dt
비변조 성분 전자 = ∫ 분광 영상의 광전자율(t) dt
전체 광전자 = 변조 성분 전자 + 비변조 성분 전자
```

두 적분은 각 시퀀스의 작성 knot와 실제 행 노출 구간을 사용한다. 합산 후 전기 readout,
정수 RAW, ISP를 수행한다. 결과에는 성분별 행 가중치·광전자·출처와 전체 전자를
분리해 남긴다. `waveform_row_effective_s`는 **변조 성분만의** 유효 시간이다.
HDR 합성이 꺼진 LFM 진단에서는 비변조 성분이 시간에 따라 변할 수 있다. HDR 합성을
켜면 두 성분 모두 각각 정적이어야 하고 PWM 노출은 매 프레임 정수 주기여야 한다.
그때 RAW 합성률은 비변조 광량과 PWM 주기 평균 변조 광량의 **합**이며, RAW만으로
각 성분을 따로 복원한 값이 아니다.

```python
test = client.post("tests", client.template(
    "temporal-test",
    assembly_id=assembly_id,
    scene_sequence_id=modulated_scene_id,
    unmodulated_scene_sequence_id=unmodulated_scene_id,
    control_sequence_id=control_id,
    capture_program_id=program_id,
))
run = client.submit_hdr(
    assembly_id, test["id"], modulated_scene_id, control_id, program_id,
    unmodulated_scene_sequence_id=unmodulated_scene_id,
)
```

파형·장면·시계는 작성한 입력이다. 실제 LED 파형, 주변광 분광량, 센서 타이밍,
움직임 정합, vendor LFM 성능이나 총 예측 불확실도는 검증되지 않았다. 두 성분의
광학 전달은 기존 분리 근사와 동일하다. 개발 소스 기능이며 기본 설치본 v0.15.0의
출시 기능이 아니다. 합성 산술·SDK→Worker·기존 단일 장면 회귀 검증은
[dev22 근거](../reports/camera_platform/physical-chain-v0161-dev22/evidence-summary.json)에 기록한다.
