# 저장된 분광 복사휘도로 카메라 응답 재계산

**현재 기본 설치본 v0.13.0에 포함된 기능입니다.** 설치본 격리·재시작과 별도 설치 QA를 확인하고, 기본 작업실의 기존 기록을 보존하여 전환했습니다. 개발 소스·Wheel·설치본·기본 작업실 실행의 근거는 아래에 구분합니다. 기본 작업실의 읽기 전용 화면 검사도 **7개 흐름·168항목**을 통과했습니다(POST 0회·브라우저 오류 0건).

완료된 장면 렌더링에서 저장한 파장별 복사휘도 `Lλ`를 사용하여, 같은 광선 격자에서 다른 카메라의 센서면 분광 조사도 `Eλ`를 계산합니다. 이후 선택한 Lens·Sensor·ISP로 RAW/RGB를 생성합니다. 원문 요구사항의 계산 의존성 기반 재사용, 분광 중간 결과 보존, 물리 입출력 계약을 연결하는 기능입니다.

```text
고정된 원본 Scene·Geometry·Material·Light와 촬영 조건
  → 완료된 Mitsuba 작업의 Lλ·MC 표준오차·깊이·원본 근거
  → 저장 광휘 재사용 작업 + 새 Lens의 명시적 Tλ·F-number
  → 새 Eλ와 새 분광 영상 자산
  → 대상 카메라의 PSF·Relative Illumination·Sensor·ISP
  → 별도 Camera Run의 RAW/RGB
```

원본 렌더링, 새 광학 변환, 후속 Camera Run은 서로 다른 작업입니다. 이 경로를 반복해도 원본 Scene과 기존 카메라 모델 버전을 수정하지 않습니다. 실행 방식은 `spectral_scene_projection`, 플러그인은 `stored_spectral_radiance_projection_v1`입니다.

## 검증 근거와 인도 상태

| 범위 | 확인한 근거 | 의미와 한계 |
| --- | --- | --- |
| 개발 소스 회귀 | [1,216개 통과](../reports/camera_platform/projection-development-tests.xml), [소스·환경 결합 기록](../reports/camera_platform/projection-development-test-binding.json) | 실패·오류·Skip 0; 실행 전후 소스/환경 불변과 JUnit SHA를 연결 |
| 별도 전환 시험 | [78개 통과](../reports/camera_platform/projection-runtime-transition-tests.xml), [직접 입력 결합 기록](../reports/camera_platform/projection-runtime-transition-tests-binding.json) | 기존 36개는 전체 회귀와 중복, 42개는 추가; 도구·시험·직접 의존 파일 5개 전후 SHA와 JUnit SHA 보존 |
| 화면 상태 회귀 | [Node 6항목 통과](../reports/camera_platform/scene-projection-ui-state-regression.json) | UI 상태 전환 로직 검사이며 실제 브라우저/Worker 시험과 구분 |
| 별도 개발 브라우저 | [26항목 통과](../reports/camera_platform/projection-ui/development/verification.json) | 실제 재사용·RAW/RGB·보존·입력 오류·화면 검사; 기본 작업실 인도 증거가 아님 |
| 설치 Wheel | [패키지 검사 통과](../reports/camera_platform/package-verification.json) | 소스·정적 파일 132개 일치, Checkout 밖 `python -I` 실제 실행, `scene_projection_api_to_raw_without_renderer_verified: true`; 기존 의존성 공유 |
| 설치본 격리·재시작 | [17항목 통과](../reports/camera_platform/projection-installed-runtime-verification.json) | 개발 Checkout 변경·재시작 뒤 원본/새 광량·MCSE·RAW/RGB 7개 해시 일치; 검사 서버 종료 확인 |
| 별도 설치 QA | [재사용 26항목](../reports/camera_platform/projection-ui/installed-v013/verification.json), [준비 20항목](../reports/camera_platform/projection-ui/preparation-v013/verification.json), [장면 작성 36항목](../reports/camera_platform/projection-ui/authoring-v013/verification.json) | 기본 작업실의 읽기 전용 화면 검증과 별도 근거 |
| 기본 작업실 전환 | [v0.12→v0.13 통과](../reports/camera_platform/primary-projection-upgrade.json) | 백업 생성, 기존 212개 기록·71개 작업·425개 감사 이벤트 보존 |
| 기본 작업실 실제 실행 | [재사용 9항목 통과](../reports/camera_platform/primary-projection-verification.json), [기존 시간 RAW/RGB 동일](../reports/camera_platform/primary-projection-legacy-rerun.json), [후속 원본 보존](../reports/camera_platform/primary-projection-integrity.json) | 새 예제·재실행으로 3개 기록·3개 작업·12개 감사 이벤트 추가; 기존 기록 보존 |
| 기본 작업실 읽기 전용 화면 | [재사용 27항목](../reports/camera_platform/projection-ui/primary-projection/verification.json), [전체 7개 흐름·168항목](camera-platform-installed-runtime.md#기본-작업실의-화면-검증) | POST 0회·브라우저 오류 0건; 기존 6개 흐름 141항목 포함 |
| 인도 입력 감사·Runtime 종료 | [14항목 확인](../reports/camera_platform/projection-release-input-audit.json), [최종 Runtime 상태](../reports/camera_platform/projection-v013-runtime-state.json) | 소스·정적 파일 132개/Wheel/설치본과 시험 결합, Manifest 688개 파일, v0.12 보관 51개 파일 보존; QA 서버·Worker 종료와 기본 작업실 정상 실행 |

원문 일곱 모듈·네 실행 방식의 전체 Goal은 미완료입니다. 실제 장비 측정은 **0건**이며 위 시험들은 구현·수치·저장·실행 경로의 근거입니다. 독립 물리 정확도나 총 불확실도를 확인한 결과가 아닙니다. 최신 기본 작업실 인도 상태는 [인도 기록](../reports/camera_platform/delivery.json)의 버전과 해시를 따릅니다.

전체 회귀 결합은 제품 Python 소스·환경을 고정한 범위이며 수집 시점의 모든 시험 파일까지 해시로 고정한 기록은 아닙니다. 별도 전환 시험은 기록에 명시한 직접 입력 5개와 JUnit을 따로 연결합니다. 이 구분은 [인도 입력 감사의 한계](../reports/camera_platform/projection-release-input-audit.json)에도 보존합니다.

## 입력과 물리적 범위

현재 변환은 다음의 명시적인 근사를 사용합니다.

```text
Eλ = π × Lλ × Tλ / (4 × N²)
Lλ: W/m²/sr/nm
Eλ: W/m²/nm
Tλ: 파장별 on-axis 투과율, 0–1
N: 대상 Lens에 고정된 F-number
```

출력의 기준면은 `sensor_ideal_image_before_psf_ri`이며, 이미 포함된 카메라 효과는 `on_axis_lens_throughput` 하나입니다. PSF와 Relative Illumination은 후속 Runtime에서 적용합니다. 픽셀 중심 pinhole 광선과 paraxial on-axis 광량 변환이라는 기존 근사 범위가 유지됩니다. 조리개 변경으로 달라지는 유한 동공 광선, 가림, 큰 defocus, 일반적인 깊이 의존 결상까지 다시 계산하는 기능은 아닙니다.

**기존 `Eλ`를 비율로 재조정하지 않습니다.** 원래 투과율이 0이었던 파장에서는 기존 `Eλ`도 0이므로, 이를 기존 투과율로 나누어 `Lλ`를 복구할 수 없습니다. 원본 `spectral-radiance.npy`에서 새 `Eλ`를 계산하여 이 정보 손실을 피합니다. RGB나 Sensor QE로 적분된 채널 영상에서 분광 정보를 추정하는 경로는 제공하지 않습니다.

재사용할 원본은 성공한 이 플랫폼의 Mitsuba Scene Render여야 합니다. 원본 작업·장면·모델 스냅샷, 입력 JSON, 모든 입력/출력 Blob의 바이트, 분광 좌표와 단위, MC 표준오차 및 보조 맵을 검사합니다. 원본 `Eλ`가 저장된 `Lλ`와 당시 투영 조건으로 다시 계산한 값과 같은지도 확인합니다. 일반 파일 업로드에 `scene-projection://` 출처만 붙여 이 검증을 우회할 수 없습니다.

| 변경 항목 | 현재 처리 |
| --- | --- |
| 파장별 on-axis 투과율 | 동일한 파장 좌표의 값과 실제 출처·방법을 명시하면 재계산 가능 |
| F-number | 대상 Lens 속성·카메라 조건이 일치하고 모델 적용 범위를 통과할 때 위 근사 안에서 가능 |
| Lens PSF·Relative Illumination 응답 | 초점거리·격자가 같고 대상 모델 계약이 맞으면 후속 Camera Run에서 적용 |
| Sensor QE·Readout·Noise 또는 ISP | 원본과 동일한 Native 격자·파장 좌표를 유지하고 실제 Runtime 호환성을 통과할 때 가능 |
| 노출·Gain | 대상 모델의 유효 영역과 고정 Gain/RAW 계약 안에서 후속 Camera Run에 반영 |
| 초점거리, Sensor Pixel Pitch, 전체 Native 높이/너비 | 원본 광선 격자와 달라 `rerender_required` |
| 파장 샘플 수·값·순서 | 원본 분광 좌표와 달라 `rerender_required`; 자동 보간 없음 |
| 온도·조명 또는 기타 전역 조건의 추가·삭제·변경 | `exposure_s`, `gain`, `f_number`를 제외하면 재렌더링 필요 |
| Scene 배치·Geometry·Material·Light 버전, 카메라 Pose | 원본에 고정됨; 이 요청에서 교체할 수 없으며 새 렌더링 필요 |
| 영상 Shape·Crop origin | 원본에 고정됨; 이 요청에서 재표본화·새 Crop 생성 불가 |

온도는 현재 카메라와 장면에 걸친 공통 조건입니다. Sensor만의 조건이라고 간주하여 변경을 허용하지 않습니다. 이전 Scene 자산을 수정하거나 다른 Scene ID를 넣어 원본 복사휘도의 의미를 바꾸는 것도 허용하지 않습니다.

대상 카메라 준비 검사는 [장면별 카메라 준비](camera-platform-scene-camera.md)와 같은 실제 Runtime 계약을 사용합니다. 같은 격자라도 파장·입사각·효과 중복·Factory 조건·ISP·Gain·RAW 형식이 맞지 않으면 `ready`가 되지 않습니다. 이후 실제 광량을 사용한 Camera Run에서도 입력과 전하 상한 등을 검사합니다.

## 화면에서 사용하는 순서

1. 장면 화면에서 성공한 원본 렌더링을 선택합니다.
2. **저장된 장면 광량 재사용**에서 적용할 카메라를 고르고 **재사용 조건 불러오기**를 누릅니다.
3. 비어 있는 파장별 투과율, 투과율 출처, 방법·버전을 입력합니다. 미확인 투과율을 1이나 기존 값으로 자동 채우지 않습니다. 출처 종류·License·Parent 등 전체 메타데이터는 조건 JSON에서 함께 검토합니다.
4. **재사용 가능 여부 검사**를 실행합니다. `ready`일 때 **센서면 광량 계산**을 제출할 수 있습니다. `rerender_required`이면 표시된 조건을 확인하고 새 Scene Render를 사용합니다. `needs_input`이면 대상 모델/조건의 문제를 해결합니다.
5. 완료된 작업의 새 센서면 광량과 원본 복사휘도 근거를 확인하고 **이 광량으로 Camera 계산**을 누릅니다. 새 분광 입력을 등록한 뒤 대상 카메라와 시험 조건으로 RAW/RGB 계산을 제출합니다.

조건을 편집하면 이전 검사가 무효화됩니다. 서버도 제출 직전에 같은 입력·원본·대상 모델·실행 환경을 다시 검사하여 `preview_sha256`가 맞을 때만 작업을 접수합니다. 조회 실패나 관찰 시간 초과는 작업 종료를 뜻하지 않습니다. 저장된 작업 ID로 상태 조회를 재개합니다. 브라우저 화면에도 저장된 계산을 다시 선택하는 경로가 있습니다.

## API와 SDK

모든 경로는 동일한 로컬 API의 세션/요청 인증 규칙을 사용합니다. SDK의 `PlatformClient`가 세션 Token을 처리합니다.

| 호출 | 입력 및 의미 |
| --- | --- |
| `GET /api/v1/scene_authoring/projection-template?source_render_id=…&assembly_id=…` | 원본과 대상 카메라로 초안 생성; 저장 없음; 투과율 값은 미입력 |
| `POST /api/v1/scene_projections/preview` | `SceneProjectionCreate` 검사; 저장 없음 |
| `POST /api/v1/scene_projections` | `{draft, preview_sha256}` 재검사 후 제출; HTTP 202 |
| `GET /api/v1/scene_projections` | 광휘 재사용 작업 목록 |
| `GET /api/v1/scene_projections/{id}` | 동일 작업 상태·결과·근거 조회 |
| `POST /api/v1/scene_projections/{id}/cancel` | 해당 작업 취소 요청 |
| `GET /api/v1/scene_projections/{id}/bundle` | 원본과 새 변환 근거 ZIP |
| `POST /api/v1/scene_projection_publications` | `{job_id, name, revision}`로 성공 결과를 분광 입력 자산으로 등록; HTTP 201 |

`SceneProjectionCreate`는 `name`, `source_render_id`, `assembly_id`, `projection`, `force`로 구성됩니다. `force` 기본값은 `false`이며 물리 입력이 아닌 실행 요청입니다. `projection`에는 명시적 광학 값과 `source_kind`, `source_uri`, `license`, `method_and_version` 등의 출처 정보가 포함됩니다. `null` 투과율은 입력 완료 전 템플릿 표현일 뿐이며 제출 가능한 수치가 아닙니다. 잘못된 타입·미입력 수치는 HTTP 계약 검증에서 거부될 수 있습니다.

실제 등록 ID와 검토한 광학 입력을 인자로 받는 SDK 예제입니다. `projection_evidence`는 선택한 대상 Lens와 파장 좌표에 맞는 `on_axis_transmittance` 및 실제 출처 메타데이터입니다. 이 예제는 측정값을 만들어 채우지 않습니다.

```python
from urllib.parse import urlencode

from camera_platform.sdk import PlatformClient


def reuse_radiance(base_url, source_render_id, assembly_id, projection_evidence):
    with PlatformClient(base_url) as client:
        query = urlencode({
            "source_render_id": source_render_id,
            "assembly_id": assembly_id,
        })
        draft = client.get("scene_authoring/projection-template?" + query)["request"]
        draft["projection"].update(projection_evidence)
        plan = client.preview_scene_projection(draft)
        if plan["status"] != "ready":
            raise ValueError(plan["issues"])

        job = client.project_scene_radiance(plan)
        print("Resume this job ID if observation times out:", job["id"])
        result = client.wait_scene_projection(job["id"], timeout=120, poll_interval=0.25)
        if result["status"] != "succeeded":
            raise RuntimeError(result.get("error") or result["status"])
        return client.publish_scene_projection(
            result["id"], "검토한 대상 카메라의 분광 입력", revision="1"
        )
```

`wait_scene_projection`은 `succeeded`, `failed`, `cancelled` 상태를 반환합니다. 관찰 시간이 끝나면 `TimeoutError`가 발생하지만 자동 취소하거나 새 작업을 만들지 않습니다. 동일한 입력의 일반 제출은 저장된 작업을 재사용하며, `force=True`는 새 실행을 요청합니다.

성공 결과 등록은 새 Scene 출처 자산과 분광 영상 기록을 한 DB 트랜잭션으로 저장합니다. 동일한 작업·내용·이름·Revision의 재시도는 같은 두 기록을 반환합니다. 이름 또는 Revision을 바꾸면 다른 등록 요청입니다. 등록은 승인이나 실물 검증을 의미하지 않으며 `approval_status`는 `draft`입니다.

## 근거와 불확실도

새 작업은 `source_render_snapshot`에 원본 렌더링 환경·입력·결과를 보존하고 자체 `environment`에 이번 광학 변환 환경을 따로 고정합니다. 과거 원본의 유효한 바이트를 재사용하는 데 당시 Mitsuba 실행 파일이나 현재 Mitsuba 프로브를 요구하지 않습니다. 새 작업을 실행하는 Worker는 새로 고정한 변환 환경과 일치해야 합니다. 원본 렌더러를 재실행한 것처럼 환경을 바꿔 기록하지 않습니다.

| 근거 | 의미 |
| --- | --- |
| `spectral-radiance.npy` | 원본 파장별 `Lλ`; 새 투과율 계산의 직접 입력 |
| `radiance-mc-standard-error.npy` | 원본 렌더링 표본에서 얻은 수치 표준오차 |
| 첫 번째 `spectral-image.npy` | 이번 투영으로 계산한 새 `Eλ` |
| `irradiance-mc-standard-error.npy` | 원본 MC 표준오차를 같은 투영 계수로 스케일한 결과 |
| `scene-projection-input.json` | 대상 카메라·투과율 출처·원본 격자를 고정한 요청 |
| 원본 렌더링 입력·요약·깊이·유효성·객체 맵 | 재사용한 장면과 광선 조건의 근거 |
| 새 영상의 `projection_job_snapshot` 및 `source_render_snapshot` | 변환 작업과 원본 렌더링을 구분하는 계보 |
| `projection_lens_model_id` | 새 입력을 생성한 대상 Lens 버전 고정 |

원본과 새 결과에 `spectral-image.npy`라는 같은 이름이 있을 수 있습니다. 근거 ZIP은 `(SHA-256, 파일명)`으로 경로를 구분하므로 이름만 보고 배열을 선택하지 않습니다. 새 작업의 첫 번째 배열이 새 `Eλ`이며, 원본 스냅샷의 경로는 원래 바이트를 가리킵니다. 새 등록 영상은 원본 렌더링의 직접 결과로 오인되지 않도록 `render_job_snapshot` 대신 위 두 계보 필드를 사용합니다.

**같은 원본으로 만든 여러 결과는 같은 Monte Carlo 표본을 공유합니다.** 각 결과를 독립 반복 측정으로 취급하거나, 각 MCSE를 독립 오차처럼 합산하면 안 됩니다. 이 값은 표본 기반 수치 오차를 스케일한 것이며 Lens 근사, 재질·광원·투과율의 오차, Sensor 모델 오차, 측정 오차 또는 모델 전체 불확실도가 아닙니다. `physical_validation: not_established`, `total_uncertainty: {kind: unknown}`을 유지합니다. `ready`와 계산 성공은 실물 예측 정확도나 제품 승인 근거를 대신하지 않습니다.

## 검증 경로

검증 구현과 통과 기록은 구분합니다. 위 표는 범위별 완료된 검증 기록이며, 아래 시험·도구는 해당 계약을 재현하는 경로입니다. 계산·설치·실행 기록을 전체 물리 검증으로 확대하지 않습니다.

- [핵심 시험](../tests/platform/test_scene_projection.py): 실제 원본 렌더링의 투과율을 0으로 두고 저장 `Lλ`에서 새 광량·RAW를 생성하는 경로, 재사용 중 Renderer 호출 금지, 격자/조건 불일치, 원본 변조 및 출처 위조 거부 등을 검사합니다.
- [원자적 등록 시험](../tests/platform/test_projection_publication.py): 동시 재시도에서 중복 없이 등록, 중간 오류 시 롤백, 같은 키의 상충 내용 거부를 검사합니다.
- [화면 검증 도구](../tools/verify_camera_scene_projection_ui.cjs): 미입력 값 차단, 변경 뒤 재검사, 실제 광량 계산과 Camera 연결, 저장 결과 재조회, 원본 렌더링 증가 여부 및 배열을 검사합니다.

Workspace 루트에서 실행하는 기능 시험:

```sh
.venv-platform/bin/python -m pytest -q \
  tests/platform/test_scene_projection.py \
  tests/platform/test_projection_publication.py
```

원본 생성 시험에는 이 프로젝트에 구성한 Mitsuba 환경이 필요합니다. 원본이 생성된 뒤 재사용 경로에서 Renderer를 호출하지 않는지를 별도로 확인합니다. 테스트에 사용하는 모델과 장면은 명시적 합성 Fixture이며 실제 장비 측정을 뜻하지 않습니다.

화면 검증은 **별도 개발 QA 서버와 그 서버의 Fixture**에서만 실행합니다. 도구가 `bootstrap.state_dir`와 Fixture의 `state_dir` 일치 및 `camera-platform-scene-qa…` 디렉터리를 확인한 뒤 기록을 만듭니다. 기본 작업실을 테스트 대상으로 바꾸지 않습니다. 아래 명령은 준비된 QA 서버·Fixture·Node/Playwright 환경을 전제로 합니다.

```sh
node tools/verify_camera_scene_projection_ui.cjs \
  http://127.0.0.1:8776 \
  reports/camera_platform/scene-camera-http-fixture.json \
  reports/camera_platform/projection-ui/development
```

Fixture에는 `state_dir`, `records.assembly`, `records.pairs[0].scene`, `records.pairs[0].render`가 필요합니다. `target_assembly_id`를 지정하면 다른 대상 카메라를 사용합니다. 도구는 `verification.json`과 화면 이미지를 출력합니다. 관찰 시간 초과 뒤에는 출력의 저장 작업 ID를 먼저 조회하며, 기존 작업의 종료를 확인하지 않고 테스트를 반복 제출하지 않습니다.

독립 실물 측정, 총 불확실도, 일반적인 Lens–Sensor 각도 결합, HDR/LFM, 전체 Agent·운영·상용화 요구사항은 이 기능으로 완료되지 않습니다. 현재 설치 검증은 공유 Python 계산 의존성을 사용하며 깨끗한 지원 OS 설치와 전체 전이 의존성 잠금을 대신하지 않습니다.
