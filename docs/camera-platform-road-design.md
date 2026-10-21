# 도로 사진 기반 상대 카메라 설계평가

도로 RGB 사진을 고정 display basis의 분광 근사 입력으로 바꾸고, 기존 DB의 렌즈 2개 × 저장 센서 모델 2개를 실제 계산한다. 입력을 결과에 맞춰 변경하지 않고, 선정용 6장과 미사용 평가용 6장으로 설계 결정을 마친다. RAW·중간 광전자·고정 ISP·반복 취득 지표와 원본 사진의 위치를 같은 package에서 검토한다.

현재 working tree의 `0.16.2.dev45`에 추가한 작업이다. 설치된 이전 서버에 이 화면과 API가 자동으로 추가되는 것은 아니다. 기존 `pyisetcam` 계산 코어, 저장 raytrace PSF, 저장 센서 snapshot, 플랫폼의 strict geometry tile capture와 ISP를 사용한다.

## 실행

저장소 루트에서 기존 platform 환경을 사용하거나 `pip install '.[platform,road-study]'`로 설치한다. `road-study` extra의 pycocotools는 공식 nuImages instance mask의 COCO RLE를 해석한다. 기존 Lens DB와 Sensor simulation DB, snapshot에 맞는 native engine이 필요하다. 실행 가능한 revision이 부족하면 다른 모델을 임의로 만들어 대체하지 않고 원인을 반환한다.

```sh
PYTHONPATH=src .venv-platform/bin/python -B tools/run_road_camera_design.py freeze --dataset-root .cache/road-design/nuimages --package reports/camera_platform/my-road-study
PYTHONPATH=src .venv-platform/bin/python -B tools/run_road_camera_design.py first-photo --package reports/camera_platform/my-road-study
PYTHONPATH=src .venv-platform/bin/python -B tools/run_road_camera_design.py run --package reports/camera_platform/my-road-study
PYTHONPATH=src .venv-platform/bin/python -B tools/run_road_camera_design.py verify --package reports/camera_platform/my-road-study
```

`freeze`는 이미지·로그 분할·마스크·카메라 revision·조건·판정 기준과 그 해시를 저장한다. 결과를 보기 전의 입력 적합성으로만 12개 서로 다른 로그를 고른다. nuImages mini의 Brown5 calibration을 지원하며 extended/fisheye calibration은 제외 이유를 보존한다. 원본 이미지는 unrectified이므로 distortion을 적용해 원본 픽셀과 물체각을 연결한다.

`run`은 중단된 package를 재개한다. 완결된 package에서는 해시를 검증하며 원본을 다시 덮어쓰지 않는다. 새로 계산할 때는 `replay`로 결과가 없는 새 디렉터리를 만든다.

```sh
PYTHONPATH=src .venv-platform/bin/python -B tools/run_road_camera_design.py replay --package reports/camera_platform/my-road-study --destination outputs/my-road-replay --stage first-photo
```

`--stage full`은 같은 frozen specification과 원본 snapshot으로 전체 계산·선정·평가를 다시 실행한다. 이미 있는 destination은 거부한다. 최초 입력 dataset 디렉터리가 없어도 snapshot을 읽으며, 계산에 쓰는 DB revision과 display asset은 보유하고 있어야 한다.

계산을 보존하고 검토 화면만 새 버전으로 만들 때는 `review --package <completed package> --destination <new directory>`를 사용한다. 원래 package와 완료 job은 바꾸지 않는다. 실패한 대비 조건의 원본 위치와 면적·픽셀 수 비용을 노출한 새 manifest를 만들며, 선정이나 계산 결과를 변경하지 않는다.

## 작업실과 SDK

별도 R1 state를 지정해 현재 소스의 서버를 실행한다. `--external-worker`를 생략하면 Worker가 함께 실행된다.

```sh
PYTHONPATH=src .venv-platform/bin/python -B -m camera_platform serve --workspace . --state-dir outputs/road-design-platform --r1-project-id road-design --port 8771 --empty
```

`http://127.0.0.1:8771/road-design.html`에서 package 경로와 `specification-binding.json`의 SHA-256으로 첫 사진 또는 전체 평가를 제출한다. Registry 조회·대기·취소는 공통 lifecycle을 사용하고, 실행 세부와 파일은 typed route에서 읽는다.

```python
import json
from pathlib import Path
from camera_platform.sdk import PlatformClient

package = Path("reports/camera_platform/my-road-study")
binding = json.loads((package / "specification-binding.json").read_text())
with PlatformClient("http://127.0.0.1:8771") as client:
    request = client.road_design_study_template()["specification"]
    request.update(name="도로 카메라 선정", package_path=str(package),
                   specification_sha256=binding["sha256"], stage="full")
    queued = client.submit_road_design_study(request)
    completed = client.wait(queued["id"], expected_kind="road_design_study",
                            timeout=14460, poll_interval=5)
    if completed["status"] != "succeeded":
        raise RuntimeError(completed.get("error") or completed["status"])
    print(completed["result"]["summary"]["viewer_path"])
    client.download_artifact(completed["artifacts"][0], "road-decision-record.json")
```

Routes:

| 목적 | API |
|---|---|
| 요청 예시 | `GET /api/v1/road-design-studies/template` |
| 제출 / 목록 | `POST / GET /api/v1/road-design-studies` |
| 실행 / package 진행 | `GET /api/v1/road-design-studies/{id}` |
| 공통 상태 / SDK wait | `GET /api/v1/jobs/{id}?expected_kind=road_design_study` |
| 취소 | `POST /api/v1/road-design-studies/{id}/cancel`, JSON `{}` |
| 보고서·원본 파일 | `GET /api/v1/road-design-studies/{id}/files/{relative_path}` |

변경된 specification, 다른 readout·ISP의 캐시, 손상된 파일, workspace 밖의 경로를 거부한다. 완료 job은 package manifest의 해시를 결과 artifact에 고정하므로 파일과 manifest를 함께 바꿔도 기존 완료 증거로 인정하지 않는다. 취소 후 새로 제출하면 검증된 완료 계산을 재사용한다. `.execution.lock`으로 같은 package의 동시 수정을 막는다.

## 물리 계산과 비교 계약

필요한 RGB 영역과 PSF·픽셀 적분 halo만 물체각에서 읽어 분광 tile을 만든다. 원본 linear RGB는 RAM에 보유하지만 업샘플한 전체 RGB나 전체 분광 cube는 만들지 않는다. 기존 CameraE2E의 strict field-dependent geometric PSF, inverse geometry, sensor spatial integration으로 native ROI의 평균 광전자를 계산한다. 이 선형 평균만 광량 sweep에 재사용한다. RAW는 각 조건과 seed에서 shot/read noise·FPN·full-well·ADC를 실제로 적용한다.

- 공통 FOV 30°, 노출 4 ms, gain 1. 광량 0.25 / 1 / 4배.
- LCD-Apple의 고정 SPD·gamma. 선형 회색 RGB 0.18을 가상 100 cd/m²로 선언한다. 450/550/650 nm의 sparse proxy이며 원래 조도·반사율의 복원이 아니다.
- 공통 bilinear ISP와 고정 WB/CCM. 센서별 chromatic transform은 장면이나 결과를 보고 조정하지 않는다. 평균 white response를 보존해 센서 밝기 차이를 없애지 않는다.
- 평가는 같은 물체각의 원본 위치에 고정한다. 공통 격자 변환 전에 anti-alias filtering과 ISP halo를 적용한다.
- CNR은 signed pre-tone linear luminance의 물체·배경 차이를 픽셀별 시간 분산의 합으로 나눈다. 공간 질감과 고정 FPN을 시간 잡음으로 사용하지 않는다.
- 32회 취득에서 시작한다. bootstrap 구간이 판정 경계에 걸린 저광량 비교에만 128회로 보강한다. 기존 RAW 파일과 처음 32개 표본은 보존한다.
- Bootstrap은 centered acquisition residual을 사용하고 `n/(n-1)`로 유한 frame 수의 분산 축소를 보정한다. 구간은 고정된 입력의 취득 잡음에 대한 불확실성이다.
- Well 포화는 native preclip charge에서, ADC 포화는 정수 RAW에서 별도로 센다. Well mask replay가 native capture metadata의 포화율과 정확히 같은지 매번 확인한다.

선정에서는 모든 사전 대비·밝은 영역 포화 제약을 만족한 후보 중 최악 저광량 CNR을 최대화한다. 차이가 불확실하면 기준안을 유지한다. 선택안과 선정 record의 hash를 고정한 뒤 미사용 6장에서는 기준안과 선택안만 계산한다. 평가용 CNR 중앙값 +10%, 개별 CNR·대비 95% 보존, 지정 밝은 영역 Well·ADC 포화 ≤1%를 확인한다. 평가를 보고 다른 후보를 다시 고르지 않는다.

대표 ROI에서 같은 물리 장면의 convolution pitch를 절반으로 바꿔 평균 광전자와 대비 변화 ≤1%를 확인한다. 원본 PSF pitch·실제 convolution pitch·native sensor pitch를 기록한다. 이는 해당 RGB 입력의 격자 안정성 확인이며 PSF refinement·native Nyquist·회절·CRA 결합의 검증으로 확대하지 않는다.

## 설계 결론을 읽을 때

렌즈 24.73/35.18 mm와 센서 pitch 3.45/2.10 µm를 공통 화각으로 비교하기 위해 가상 sensor extent를 사용한다. 30°에서 약 13.25×7.46 / 18.86×10.61 mm가 필요하다. 긴 초점거리의 이득에는 약 2배 면적과 더 많은 native pixel의 비용이 포함된다. 실제 센서 제품의 active-array에 맞는 화각·장착 가능성을 확인한 결과가 아니다. 보고서는 면적과 전체 좌표계의 MP를 공개하며 전체 RAW를 촬영한 것처럼 표시하지 않는다.

저장 PSF는 기하 ray histogram이고 투과율·상대 조도는 unity 가정이다. 센서 전기 값은 저장된 추정값이다. 원본 카메라의 흐림·잡음·sharpening·포화가 RGB 입력에 남아 있다. 자연 영상의 선 프로파일과 CNR을 렌즈 MTF·검출기 mAP로 부르지 않는다. 낮은 절대 CNR에서는 상대 개선만으로 물체 인식을 보장할 수 없다.

입력은 [nuImages 공식 이용 안내](https://www.nuscenes.org/nuimages)의 비상업 연구 범위로 사용한다. 상업적 dataset 권한은 확인되지 않았다. 배포 package에 원본 이미지가 포함돼 있으므로 이용 조건을 유지해야 한다. 실측·새 FDTD·HDR/LFM·검출기 학습·제품 출시의 완료를 주장하지 않는다.

## 주요 모듈

| 모듈 | 역할 |
|---|---|
| `road_rgb_source.py` | 원본 intrinsics/distortion, fixed display basis, RGB strict geometry tile provider |
| `road_design_inputs.py` | 입력만으로 이미지·마스크·DB 후보 선택, immutable specification |
| `road_design_compute.py` | native mean·고정 FPN·fresh RAW·고정 ISP·공통 좌표 |
| `road_design_statistics.py` | 시간 잡음 CNR·bootstrap·제약 선택·독립 평가 |
| `road_design_study.py` | 캐시 검증·재개·선정 lock·전체 package 검증 |
| `road_design_report.py` | 원본 위치·마스크·선 프로파일·비용·결정 보고서 |
| `road_design_job.py` | Registry/Worker·typed API·완료 파일 binding |
| `tools/run_road_camera_design.py` | freeze / first-photo / run / verify / replay 진입점 |
