# Camera Platform dev25: 저장 Lens DB 처방의 회절 경로 적합성 검사

이 문서는 dev25 당시의 판단 기록이다. 정확한 특허 원문 대조로 직경과 28번째 행의 역할을 바로잡고 OPD를 실제 실행한 결과는 [dev26 문서](camera-platform-joint-workflows-dev26.md)를 따른다.

이번 단계의 결론은 명확하다. **현재 저장 Lens v2 기하 PSF를 회절·광량·CRA가 검증된 제품 PSF로 승격하지 않았다.** 대신 실제 Lens DB revision 한 건의 원본 처방을 기존 RayOptics/Prysm 경로에 빠짐없이 옮기는 사전 검사를 구현하고, 계산 가능한 부분과 차단해야 할 부분을 수치와 출처로 분리했다. 사용자 장면→RAW와 저장 D65 RAW→기존 ISP의 별도 설치본 경로는 [dev24 계약](camera-platform-joint-workflows-dev24.md)과 동일하게 유지한다.

`lens_db_wavefront_bridge.db_wavefront_preflight()`는 Lens catalog revision, SQLite DB 해시, 검토 CSV 원본 바이트와 SHA-256, DB/CSV 처방 fingerprint가 일치하는지 확인한다. `p0014:intermediate`의 번호 매긴 28개 행, 네 면의 A10 항, stop, 마지막 `image` 표기 행의 0.50 mm 간격을 누락하지 않는다. `effective_aperture`를 지름으로 해석하는 것은 **기존 기하 PSF 생성기에서 물려받은 미검증 가정**으로 명시하고, `nd/Vd`의 Buchdahl2 분산도 측정 유리 데이터가 아닌 근사로 기록한다. 새 `camera_even_asphere_v2`는 A4/A6/A8/A10과 단위를 명시적으로 받으며 기존 v1 계약을 유지한다. Factory와 간격 인증기의 면 수 상한은 40으로 확장했고, 기존 ray/interface 방문·시간 예산은 유지한다.

550 nm RayOptics **근축 변환 진단**의 EFL은 35.1784659406 mm로 저장 기하 PSF 생성 기록과 정확히 일치한다. 하지만 입사동공 반경은 4.9495453223 mm 대 저장 기록 4.8723637233 mm로 **1.584% 다르다.** 기존 생성기는 선언한 image f/3.61을 사용하고 OPD 경로는 원본 stop 반경을 동공에 맵핑한다. 따라서 같은 렌즈의 광학 PSF라고 간주할 근거가 없다. 이 진단은 OPD·회절·RAW를 계산하지 않는다.

완전한 처방을 기존 `prescription_psf`에 제출하면 마지막 번호 매긴 `image` 행의 semi-diameter 25 mm와 후단 간격 0.50 mm 때문에 보수적 영상측 각도 상한이 **88.8702°**가 된다. Factory의 지원 영역 80°를 넘으므로 `rear_angular_bound_exceeds_factory_domain`으로 거부하고 `psf_request=null`을 반환한다. 이 행을 임의로 빼거나 구경을 줄여 계산을 성공시키지 않았다. 더 짧은 검토 처방 `p0349`도 필수 aperture 20개가 없어 추정값으로 채우지 않고 차단한다. 유효한 처방이 확보돼 사전 검사를 통과하면 `characterize_db_wavefront_revision()`이 기존 RayOptics/Prysm 계산을 호출할 수 있지만, **현재 확인한 DB revision에서는 회절 PSF 출력이 없다.**

[dev25 wheel](../reports/camera_platform/integrated-workflows-20260928/pyisetcam-0.16.2.dev25-py3-none-any.whl)의 SHA-256은 `e01f8f3a89417e19d18cd1f3c2cd0c9b3fd42f53f50531ee638679257569fdc4`다. 별도 설치 경로의 1,037개 파일을 wheel과 바이트 대조하고 새 Registry·child Worker에서 [설치본 증거](../reports/camera_platform/integrated-workflows-20260928/joint-dev25-radiance-isp-wavefront-installed-proof.json)를 만들었다. 여기에는 사용자 분광 장면의 8×8 정수 RAW, **다른** D65 저장 RAW의 기존 ISP 재처리, 대각 화각 초과 입력의 제출 전 거부, DB 처방의 차단·근축 진단이 각각 기록돼 있다. 설치본의 `physical_validation=not_established`, `product_ranking_allowed=false`, `diffraction_psf_computed=false`를 유지한다. 기존 8770 실행 서버를 교체했다는 증거는 아니다.

다음 광학 작업은 p0014 마지막 영상면 행이 실제 굴절 인터페이스인지, 센서 평면까지의 거리와 `effective_aperture`의 반경·직경 규약을 원본 자료로 확인하는 것이다. 그 뒤 같은 pupil·초점·개구·field·파장으로 OPD와 PSF의 수치 수렴 및 광량을 다시 계산해야 한다. 별도로 센서 구조에 맞는 파장×입사각 native response/LUT가 없으므로 CRA×픽셀 결합과 제품 순위는 계속 막는다. [dev20 기록](camera-platform-joint-workflows-dev20.md)의 MTF 픽셀 적분 면적, HDR/LFM 유효 coverage·제품 timing, 인지 정답의 독립성, 범용 고급 ISP와 다목적 최적화도 아직 완료되지 않았다.
