# 다음 범위: 기준 노출을 보존하는 RAW 합성과 움직임·주기광 진단

이 문서는 다음 구현 범위의 제안이다. 작성 시점의 개발 버전은 `0.16.0.dev6`이며,
아래 새 Runtime과 계약은 아직 구현되지 않았다. 기존 일곱 모듈·네 실행 방식과
일반 광학·픽셀·전기 모델 확장 목표를 축소하거나 완료로 선언하지 않는다.

## 현재 확인한 경계

`hdr_bracket_v1`은 이미 움직이는 분광 영상 timeline, native global/rolling 노출,
정확한 PWM 적분, 전기 readout, 실제 integer RAW와 RGB를 연결한다. 다만 합성을
켜면 하나의 정적 optical image와, PWM인 경우 모든 노출의 정수 주기를 요구한다.
`hdr.merge_raw_bracket`의 시간 일관성은 진단 값이며 현재 합성에서 불일치 표본을
제외하지 않는다. 정적 입력을 강제하는 Runtime의 경계가 이 단순 합성을 보호한다.

따라서 정적 입력 검사를 삭제하는 것만으로 움직임 HDR을 제공하면 안 된다.
정수 PWM 주기를 노출하더라도 움직이는 영상의 시간 변화와 PWM이 상관되므로,
같은 LED ON 시간은 같은 적분 전하를 보장하지 않는다.

개발 소스의 실제 순수 Runtime에서 이를 확인했다. 기본 moving-step timeline,
잡음 없음, identity PSF/RI/angular response, global shutter, 2 ms 노출,
1 ms 주기·50% duty PWM에서 위상을 0에서 0.5 ms로 바꾸었다.
각 row의 effective exposure는 같았지만 다음 결과가 달랐다.

- 최대 적분 전하 차이: `3.3225169345782035 electron/pixel`
- 변경된 RAW 표본: `896 / 8192`
- 변경된 RGB 표본: `2880`
- 첫 RAW C-order bytes SHA-256:
  `488ba507d18685d2ab8c77f430a9a5a1df8b39ca386b475dcea633a8e23c9237`
- 위상을 바꾼 RAW C-order bytes SHA-256:
  `a9f5c170f4ab6cf61f379df34410c33f968129327e16336cb746ae3bf7e03b20`

같은 조사에서 기존 `tests/platform/test_hdr_runtime.py`는 25개가 통과했다.
이는 authored 계산 경로의 확인이며 실물 센서나 상용 LFM 성능 근거가 아니다.

## 제안하는 최소 구현

새 Runtime `reference_hdr_v1`을 추가하고 기존 `hdr_bracket_v1` 계약과 출력은 유지한다.
사용자가 선택한 짧은 기준 노출의 유효 범위를 보존하면서, RAW가 일관되는 정적
배경에서는 긴 노출의 잡음 이점을 이용한다. 움직임 등으로 RAW 전자율이 서로
맞지 않는 곳은 기준 노출 하나로 돌아간다. 이를 정합 또는 완전한 deghosting으로
표시하지 않는다.

프로그램 스키마는 `camera_reference_hdr_capture_program_v1`이며 필드는 정확히
`schema`, `electrical_model`, `waveform`, `hdr_merge`다. 전기 모델은 기존 계약을
재사용한다. `waveform`은 `null` 또는 명시적인 constant만 허용한다. 시간에 따라
달라지는 PWM은 기존 no-fusion 획득·진단 경로로 남긴다.

```json
{
  "schema": "camera_reference_hdr_merge_v1",
  "reference_frame_index": 0,
  "base_profile": "UNCHANGED camera_hdr_merge_v1 OBJECT"
}
```

`reference_frame_index`는 실제 제어 시퀀스의 유효 index여야 한다. 결과는 해석된
frame ID와 index 둘 다 기록한다. 프로그램을 적용하는 Test가 정확한 제어 시퀀스와
프로그램 해시를 고정하므로 같은 index가 다른 프레임을 묵시적으로 뜻할 수 없다.

새 순수 합성 함수는 원본 integer RAW, 노출 시간, 기존 아홉 calibration 항목과
native geometry만 받는다. 장면, noiseless 전하, simulator clipping truth, 정답
움직임은 입력으로 받지 않는다.

1. 기존 RAW 전자율·plug-in 분산·linear censoring을 계산한다.
2. 유효한 노출 쌍의 모든 차이를 기존 noise-scaled consistency 식으로 비교한다.
3. 불일치를 reflected 3×3 이웃으로 확장한다. 이는 bilinear RGB의 이웃 의존성을
   고려하는 보수적인 고정 정책이며 optical flow가 아니다.
4. 기준 노출이 유효하고 확장된 불일치가 없으면 usable 노출을 inverse-variance로
   합성한다. 불일치가 있으면 기준 노출 가중치만 1로 둔다.
5. 기준 RAW가 무효면 다른 프레임이 밝은 영역을 보여도 가중치를 전부 0으로 두고
   radiometric 결과를 NaN, validity를 false로 유지한다.
6. 한 노출만 유효한 곳의 시간 일관성은 unknown으로 남긴다. preview에서만 무효
   값을 검정으로 표현하고, signed 선형 값과 masks는 그대로 보존한다.

이 범위는 기준 노출이 포화된 영역의 dynamic range를 늘리지 않는다. 기준 노출의
밝은 영역 headroom을 유지하며 일관되는 어두운 영역의 noise를 줄이는 것이 이익이다.
불일치는 움직임, 광원 변화, noise 또는 calibration error일 수 있으므로 motion mask나
움직임 검출 정확도로 이름 붙이지 않는다. 분산은 선택된 가중치에 조건부인 plug-in
추정이며, data-dependent 선택의 편향·ghost 확률·total uncertainty를 설명하지 않는다.

## 파일과 통합 범위

- `reference_hdr.py`: 위 RAW-only 합성, 엄격한 profile, 명시적 masks와 summary.
- `reference_hdr_runtime.py`: 기존 HDR no-fusion capture를 호출한 뒤 동일 RAW에 새
  합성을 적용한다. 새 계약을 정규화하고 정상광·기준 index·전체 timing을 검사한다.
- `capture_programs.py`, `runtime.py`, `contracts.py`: 별도 schema/plugin/template mode.
- `sequences.py`, `service.py`, `imaging.py`, `sdk.py`: 원본 프로그램·시퀀스·Test·RAW
  replay의 고정 계약, cache/Worker/evidence 경계, 공개 API 실행 경로.
- Workbench: 기준 프레임과 RAW 일관성/불일치/unknown, 기준-only fallback, 유효 범위를
  보여준다. 상용 LFM 또는 물리 검증이라는 표현은 사용하지 않는다.
- 사례: 고정 설계와 기준 노출, 합성 이익·fallback 영역, 남은 motion/PWM ambiguity,
  다음 독립 paired capture를 연결한다.

새 wrapper는 기존 `hdr_runtime.py`의 no-fusion 수치 경로를 사용하며 기존 v1 구현을
넓히지 않는다. v1 실제 RAW/RGB/합성 bytes를 변경 전후 비교한다.

## 필요한 검증

- 독립 scalar 식으로 noise-scaled pairwise gate와 최종 weights/rate/variance 확인.
- 정적 배경의 multi-exposure 합성과 움직이는 밝은 물체의 기준-only fallback을 실제
  integer RAW로 확인하고 naive 합성과 선형 RGB 차이를 측정.
- 기준 포화·하한 clipping·모든 표본 무효·하나만 유효·음수 신호·native CFA 네 위상.
- simulator oracle을 훼손해도 RAW와 calibration이 같으면 합성 결과가 같음.
- 정상광 외 waveform, 잘못된 기준 index, 잘못된 calibration, timing overlap, 출처
  clock 불일치, geometry drift, resource budget 초과를 캡처 전에 거부.
- 움직임×PWM 위상 반례를 회귀로 보존하고, 이 조건은 no-fusion 경로만 실행 가능함.
- API→별도 Worker→원본 RAW→새 합성→Test/사례→bundle→restart readback→실제 CUA.

독립 측정과 uncertainty 확립, subpixel registration, intra-exposure blur, occlusion,
rolling motion warps, 별도 ambient/LED 성분, vendor LFM, shared-charge DCG와 bracket의
복합 획득은 후속 범위다. 기존 설치본 v0.15와 개발 Runtime의 상태를 구분한다.
