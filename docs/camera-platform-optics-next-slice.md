# 다음 광학 확장: 동공 투과율과 절대 광량

2026-09-20 당시의 읽기 전용 설계 검토를 보관한 문서입니다. 아래 stop transmission
기능은 이후 `lens_factory.py`, `prescription_psf.py`, `projection_radiometry.py`에
구현되었습니다. 이 문서의 "아직 구현되지 않았다"는 상태는 더 이상 현재 상태가
아닙니다. 현재 source 위치 RI 범위와 남은 물리 한계는
[소스 위치 상대 조도 연산자](camera-platform-source-position-ri.md)를 참고하세요.
사용자가 우선한 일반 광학·픽셀 모델 확장의 다음 작은 단위로, 명시한 수동 stop
투과율 분포를 회절 PSF와 광량 계산에 함께 적용합니다.

## 현재 공백

`lens_factory.py`는 통과 여부와 quadrature power로 광량을 계산하고,
`prescription_psf.py`는 통과한 동공에 균일한 진폭과 OPD 위상을 적용합니다.
정규화된 PSF만 바꾸면 투과율에 따른 광량 손실이 없어집니다. 또한 기존 장면
투영의 `pi/(4N²) * T`와 새 절대 광량을 함께 적용하면 손실을 중복 계산합니다.

## 제안 계약과 연결

새 `pupil_transmission.py`에서 `camera_stop_transmission_v1`을 정의합니다.
stop surface ID, 파장, 0–1 정규화 반경, 파장·반경별 power transmittance
`T ∈ [0,1]`, power 기준 선형 보간과 출처를 보존합니다. 기존 v1 요청의 의미를
바꾸지 않고 새 Factory 요청 버전으로 연결합니다.

- `lens_factory._pass`: 실제 stop 교차 위치에서 평가한 `T`를 광선 power에 적용합니다.
- `prescription_psf._wavefront/_propagate`: 같은 `T`의 제곱근을 복소 동공 진폭에 적용합니다.
- Factory publication: 처방·mask hash가 같은 절대 광량과 conditional PSF만 묶습니다.
- Projection: 같은 모델의 절대 광량을 한 번만 소비하며 기존 독립 throughput과 혼용하지 않습니다.
- Sensor: 기존 각도 가중치와 상대 QE를 사용합니다. QE에 포함된 CFA·수집효율과
  pixel-integrated PSF의 aperture를 다시 곱하지 않습니다.

## 통과 기준

1. `T=1`은 기존 결과를 유지합니다. 균일 `T=.25`는 정규화 PSF 모양을 유지하고
   절대 광량·평균 광전자량을 ¼로 만듭니다. 잡음을 끈 RAW는 양자화 오차 범위에서 일치합니다.
2. 환형·완만한 투과 분포를 독립 pupil DFT·에너지 계산과 비교합니다. PSF, throughput,
   각도 가중 QE가 일치하고 두 단계 수렴 및 기존 tail/cone/mapping 제한 검사를 통과합니다.
3. 실제 Factory→Lens→재투영→RAW 경로를 실행합니다. 같은 입력의 재현성, 다른 입력의
   새 근거, 이전 실행의 불변성을 확인하고 stop·파장·mask hash 불일치 및 광량 중복 적용을 거부합니다.

이 범위는 명시한 수동 차광·투과 모델입니다. 유한 물체거리, Fresnel/coating,
편광, 일반 pupil Jacobian 전달은 별도 확장입니다. 현재 픽셀 물리 응답의 4개 native
site·방위각 정보를 Runtime의 3색·입사각 응답으로 연결하는 한계도 별도로 해결해야 합니다.
