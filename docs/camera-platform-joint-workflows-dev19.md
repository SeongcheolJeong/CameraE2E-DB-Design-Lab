# Camera Platform dev19: 설치본 검증 중 드러난 동일 면적 MTF 공백

`0.16.2.dev19`는 저장 Lens×Sensor PSF RAW의 화질 Job에 공통 550 nm 물체각 **균일 정사각형** 격자 MTF 진단을 추가하고, 작은 합성 인지 박스의 출처를 `synthetic_contract_fixture`로 구분했다. 기존 CameraE2E의 기하 raytrace PSF convolution과 원본 RAW 보존은 유지했다.

[dev19 wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev19-py3-none-any.whl)의 SHA-256은 `7f572adbcfd0af2475dc1e1b6fcfe243e25df0734fef1912dc9bf0a081a57ce2`다. 격리 설치본에서 [인지 코호트](../reports/camera_platform/integrated-workflows-20260928/joint-dev19-perception-installed-proof.json)는 실행됐고, 진단 mAP 0.667과 `product_map.available=false`를 확인했다.

같은 설치본의 Lens×Sensor 2×2 화질 Job 자체는 완료됐지만 **새 균일 격자 MTF는 `available=false`**였다. 가장 거친 센서의 검증된 native 중앙 ROI가 16×16이라, 모든 영상의 유효 영역 안에 새로운 균일 16×16 격자를 만들 공간이 없었다. 합성 단위 시험만으로 이 지표를 사용 가능하다고 판단한 것이 부족했다. 설치본 검증기는 이를 통과 처리하지 않고 실패했다. 결과는 제품 순위에 사용하지 않았다.

[dev20](camera-platform-joint-workflows-dev20.md)은 이 실패 원인을 반영해 가장 거친 카메라의 **실제 검증된 픽셀 중심 좌표**를 공통 표본 격자로 쓰는 보수적인 후속 경로를 별도 wheel로 제공한다. dev19 wheel과 성공한 인지 증거는 수정·덮어쓰지 않는다.
