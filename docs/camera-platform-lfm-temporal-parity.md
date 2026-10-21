# HDR/LFM: Lens × Sensor PWM 시간 적분 교차 검사

기존 결합 LFM 경로는 정적 Lens × Sensor 광학 응답에 저/고 조도 비율과 정확한 직사각 PWM 행별 ON 시간을 곱한 뒤, 저장 Sensor readout으로 RAW를 만든다. `lfm_temporal_parity.crosscheck_pwm_temporal_capture()`는 **한 노출·한 작성 위상**에서 이 행별 전자 평균을 별도의 정수 ns 적분(`periodic_light.exact_on_duration_ns`) 및 `pyisetcam.sensor_compute_temporal`과 비교한다. 시간 엔진은 광학 응답을 유한 횟수 샘플링하고 표본 사이를 선형 보간하므로 직사각 에지의 수치 오차가 남는다. 결과에는 두 표본 밀도와 오차를 모두 기록하고, 선언한 허용 오차 안일 때만 `passed_at_declared_numerical_resolution`을 반환한다.

오프라인 명령 `tools/verify_camera_lfm_temporal_parity.py`는 저장 Lens/Sensor revision을 다시 로드하고, 현재 결합 LFM 공식의 전자 평균을 재계산해 위 비교에 넣는다. 원래 저장된 LFM NPZ를 검사하는 명령은 아니다. 출력의 `saved_lfm_artifact_compared=false`가 그 경계를 기록한다. 정확한 입력 ns, 광량, DB revision, 근사 렌즈 광량 허용 여부를 명령에 명시해야 한다. 예를 들어 [dev20의 고정된 검사 대상](../reports/camera_platform/integrated-workflows-20260928/joint-dev20-hdr-readiness-installed-proof.json)에 대해 실행한 명령은 다음과 같다.

```sh
PYTHONPATH=src .venv-platform/bin/python tools/verify_camera_lfm_temporal_parity.py \
  --lens-revision-id 4daeca8f-5d81-57b0-8ae3-928f23de2b36 \
  --sensor-identifier DEF-2512-801 \
  --sensor-run-id 360dfe5370ada20b81c085d5859e9b0ae54be66df0a5e7e8eed0bffe7c6dc601 \
  --root camerae2e_db --workspace . \
  --reference-exposure-ns 4000000 --checked-exposure-ns 1000000 \
  --period-ns 2000000 --on-time-ns 1000000 \
  --phase-offset-ns 500000 --line-time-ns 20000 \
  --off-to-on-ratio 0.02 --on-luminance-cd-m2 1000 \
  --accept-approximate-lens-radiometry \
  --allow-numerical-negative-photons \
  --output reports/camera_platform/lfm-temporal-parity-20260928/dev20-pinned-phase-parity.json
```

[실행 증거](../reports/camera_platform/lfm-temporal-parity-20260928/dev20-pinned-phase-parity.json)의 재계산 LFM 평균과 정수 시간 기준의 최대 차이는 ON 신호 대비 `4.31e-10`이다. 시간 엔진과 정수 기준의 최대 차이는 노출당 128표본에서 `0.0035575`, 512표본에서 `0.0006880`으로 줄어, 선언한 `0.003` 허용 오차를 512표본에서 충족했다. 저장 readout 설정은 시간 엔진 출력에서 일치한다. 이 검사는 확률적 RAW 바이트가 같다는 주장이나 독립 측정 도구와의 비교가 아니다.

저장 기하 PSF의 광자 배열은 1104×1104×3이며, 양의 최대 `1.65e15`에 비해 매우 작은 음수 샘플 90,903개(최소 `-0.0006309`)를 포함한다. `pyisetcam` 시간 엔진은 음수 광자를 거부한다. 따라서 기본 동작은 거부이고, 위 명령은 명시적 허용 후 **메모리 사본만** 0으로 보정했다. 이 경로는 음수 크기가 양의 최대치의 `1e-12` 이하이고 보정 전후 Sensor 전자 평균 차이가 `1e-10` 이하일 때만 진행한다. 이 입력의 음수 크기 비율은 `3.82e-19`, Sensor 전자 평균 변화는 계산 정밀도에서 0이었다. 원본 광학 배열이나 저장 자산은 변경하지 않았다.

이 첫 검사의 범위는 작성된 한 PWM 위상, 정적 장면, virtual 8×8 Sensor 창이다. 원본 센서의 die crop 위치·row-zero와 PWM 절대 위상, 실제 LED 에지·스펙트럼, 움직임, 제품 HDR/LFM 촬영 순서와 timing 값은 확인하지 않았다. 기존 HDR coverage가 `inconclusive`라는 [dev20 판단](camera-platform-joint-workflows-dev20.md)을 바꾸지 않는다. 증거는 `physical_validation=not_established`, `product_hdr_lfm_decision=not_established`를 유지한다. 아래 검사는 새로 저장한 LFM 원본의 두 노출·네 위상을 추가로 다룬다.

## 저장 원본 LFM의 2노출 × 4위상 검사

`load_verified_lfm_artifact()`는 신뢰할 JSON SHA-256, JSON에 봉인된 NPZ SHA-256, 원본 `original_pwm_raw_dn`의 여덟 위상 해시, 행 노출 창, 적용 readout·seed를 재검사한다. `crosscheck_pwm_four_phase_set()`은 선택한 한 노출의 네 위상 모두에서 저장 `integrated_mean_electrons`를 정수 ns 기준과 시간 엔진에 비교한다. 각각의 노출을 검사해 전체 **2노출 × 4위상** fixture를 덮었다. 자료는 기존 dev20 저장 파일을 재발견한 것이 아니라 현재 코드로 새로 실행한 [원본 LFM 보고서](../reports/camera_platform/lfm-temporal-parity-20260928/dev28-source-lfm/lens-sensor-lfm.json)와 [NPZ](../reports/camera_platform/lfm-temporal-parity-20260928/dev28-source-lfm/lens-sensor-lfm.npz)다. 이 둘의 SHA-256은 각각 `ff1b7f6d6513d7b949cfd417755f915cd07e6bfd55c58d5c253fe6d5f571ccc6`, `94a35f9c8b373732ddc68238f31e35607d0255bcd7930d0cd3020921559e1a6f`이다.

```sh
PYTHONPATH=src .venv-platform/bin/python tools/verify_camera_lfm_four_phase_parity.py \
  --lfm-report reports/camera_platform/lfm-temporal-parity-20260928/dev28-source-lfm/lens-sensor-lfm.json \
  --lfm-npz reports/camera_platform/lfm-temporal-parity-20260928/dev28-source-lfm/lens-sensor-lfm.npz \
  --report-sha256 ff1b7f6d6513d7b949cfd417755f915cd07e6bfd55c58d5c253fe6d5f571ccc6 \
  --root camerae2e_db --workspace . --exposure-index 0 \
  --samples-per-exposure 64 256 --maximum-relative-error 0.003 \
  --accept-approximate-lens-radiometry --allow-numerical-negative-photons \
  --output reports/camera_platform/lfm-temporal-parity-20260928/dev28-four-phase-saved-artifact-parity.json
```

두 번째 노출은 `--exposure-index 1`과 다른 출력 경로로 같은 명령을 실행했다. 기존 증거 파일 보호를 위해 명령은 출력 경로가 이미 있으면 덮어쓰지 않는다.

| 원본 노출 | 네 위상 중 최대 저장 평균 오차 | 256표본 시간 엔진 최대 오차 | 증거 |
| --- | ---: | ---: | --- |
| 1 ms | `6.61e-10` | `0.0016445` | [네 위상 결과](../reports/camera_platform/lfm-temporal-parity-20260928/dev28-four-phase-saved-artifact-parity.json) |
| 4 ms | `3.59e-10` | `0.0010415` | [네 위상 결과](../reports/camera_platform/lfm-temporal-parity-20260928/dev28-four-phase-long-exposure-parity.json) |

여덟 위상이 선언한 `0.003` 수치 허용 오차를 충족했다. 하지만 시간 엔진의 표본 기반 확률적 RAW 해시는 저장 원본 RAW 해시와 같지 않았다. 따라서 이 결과는 **저장 전자 평균의 시간 적분 일치**이며 원본 RAW의 바이트 재현이나 HDR fusion 일치가 아니다. 원본 LFM 보고서에는 원래 광학 이미지의 전체 광자 바이트 해시가 없으므로 재계산한 Lens/Sensor/PSF/자산 binding은 검증해도 `original_optical_image_bytes_reverified=false`로 남긴다. 두 결과 모두 `product_hdr_lfm_decision=not_established`다. 전체 위상 공간, 움직이는 장면, 실제 센서 timing과 절대 위상은 여전히 미검증이다.
