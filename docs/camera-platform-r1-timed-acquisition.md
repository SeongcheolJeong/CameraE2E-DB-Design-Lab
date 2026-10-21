# R1 등록 자산 시간 촬영 파일럿

`r1_timed_acquisition`은 설치된 R1 합성 예제의 등록 모델과 장면 ID를 사용해
시간별 이상영상, 기록된 적용 노출·gain, 선택적 PWM 파형으로 원본 정수 RAW를
생성하는 별도 Job이다. 현재 R1 RC의 정식 지원 선언인 정적 설계 실험을 확대하지
않는다. 실물 카메라 예측 정확도, vendor LFM, 움직임 HDR 보정은 입증되지 않았다.

R1 프로젝트 모드의 `GET /api/v1/r1-timed-acquisitions/template`은 등록 자산
목록과 완전한 제출 예제를 반환한다. `acknowledge_simulation_limits`를 `true`로
설정한 뒤 `POST /api/v1/r1-timed-acquisitions`에 제출한다. `GET
/api/v1/jobs/{id}`로 Worker 상태를 보고, `GET
/api/v1/r1-timed-acquisitions/{id}`에서 원본 RAW·프레임별 RGB·시간 기록과
artifact URL을 재조회한다. 목록과 취소 경로도 같은 접두사를 쓴다.

입력은 등록 모델 하나, 등록 장면의 시간 knot 2..8개, 기록된 프레임 1..4개로
제한된다. knot의 `time_ns`는 증가하는 상대 정수 시각이며 각 장면에 유한
`radiance_scale`을 준다. `control_sequence`의 clock과 각 프레임의 실제 적용
노출·gain·ISP ID가 필요하다. 노출은 0.05..20 ms, gain은 0.25..8이며 ISP는
고정 `r1-fixed-isp-v1`이다. 시간 적분과 전기 RAW 취득은
`hdr_bracket_v1` 런타임에서 수행하며 기본 상태는 HDR 병합 비활성이다.
PWM은 명시적 주기, ON 시간,
위상 원점, low/high 수준과 같은 clock을 요구한다.

`merge_hdr=true`는 둘 이상의 서로 다른 독립 노출과 **같은 등록 장면·같은
radiance_scale**을 요구한다. 런타임은 각 knot의 광학 영상이 실제로 동일한지
다시 검사하며, PWM이 있으면 모든 브래킷 노출이 전체 주기의 정수배여야 한다.
조건이 어긋나면 제출 전 병합을 거부한다. `merge_hdr=false`에서는 움직이는
선언 영상이나 부분주기 PWM의 원본 RAW 및 행별 파형 노출 비율을 보존할 수 있다.
이는 파형 진단이며 LFM 합성 영상이나 vendor anti-flicker 성능 결과가 아니다.

Job은 모델·장면과 광학·픽셀·전기 component의 ID/revision/hash, 고정 ISP,
프로젝트 모드, 실행 환경, 시간 입력을 동결한다. Worker가 동일한 입력을 다시
검증한 다음 계산한다. 원본 `original-raw-frames.npy`는 병합 전 int32
`[frame,16,16]` 배열이고, HDR 결과가 있으면 `hdr-rgb.npy`와 유효 mask를
별도 저장한다. 재조회는 결과 JSON과 배열 artifact의 해시·shape·dtype을
검증한다. 등록 예제는 16×16·500/600 nm 합성 입력이며 3D 기하학적 움직임,
실물 시계 정확도, Lens DB 원본 광학·센서 동등성을 주장하지 않는다.

SDK에서는 `PlatformClient`로 같은 작업을 제출·대기할 수 있다.

```python
spec = client.r1_timed_acquisition_template()["specification"]
spec["acknowledge_simulation_limits"] = True
job = client.submit_r1_timed_acquisition(spec)
result = client.wait(job["id"], expected_kind="r1_timed_acquisition")
```

개발 검증: `.venv-platform/bin/python -m pytest -q tests/platform/test_r1_timed_acquisition.py`.
[dev31 wheel 격리 설치 증거](../reports/camera_platform/integrated-workflows-20260928/r1-timed-dev31-installed-proof.json)는
SDK 제출·대기, 별도 Worker, 정적 HDR와 동적 RAW의 원본 재조회까지 확인했다.
실제 네트워크 서버, 물리 측정 비교와 제품 모드 검증은 별도 출시 관문이다.
