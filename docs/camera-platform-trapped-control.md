# 트랩 상태를 포함한 실제 전기 AE와 코드 영역 ISP

이 문서는 `0.16.1.dev9`의 계산 계약을 설명한다. 대상은 저작한 1종 트랩의 capture/release와 PD 불완전 전송을 함께 계산하는 시뮬레이션이다. 실제 CIS의 트랩 파라미터를 측정·보정한 모델, TG 전위 장벽 해석, TCAD 또는 검증된 소자 SRH 모델을 뜻하지 않는다. 배포·브라우저·실물 검증 여부는 아래 증거 상태와 별도로 판단한다.

## 고정된 입력과 실제 획득의 구분

새 획득 프로그램은 `camera_electrical_control_capture_program_v3`이며 기존 `electrical_control_v1` 실행 경로를 사용한다. 기존 AE 프로그램 v1(단일 경로), v2(공유 전하 HCG/LCG)는 기존 비트·계약 호환성을 유지한다. v3는 다음을 명시적으로 고정한다.

- `retained_charge_model`: `camera_trapped_photodiode_v1`의 PD 용량, 4개 native Bayer site별 transfer efficiency, 트랩 용량, capture rate, release time 및 적분 한계.
- `observation_readout_id`: `single`, `HCG`, `LCG` 중 실제 전기 모델에 존재하는 한 경로. 단일 경로에서도 생략하지 않는다.
- `electrical_control_policy`: ROI, 측광 site, 부호 있는 black-relative DN 목표, 정수 ns 노출 격자, 한 번에 변경 가능한 비율, 프레임 지연 및 선언한 처리·actuator 지연.
- 기존 control sequence: 프레임 시작·readout mode 및 공통 초기 설정. 이후 실제 노출을 미리 정한 결과로 해석하지 않는다.

선형 단일, 비선형 단일, 선형 공유 전하 HCG/LCG, 비선형 공유 전하 HCG/LCG를 지원한다. 공유 경로는 한 번 생성·트랩 교환·전송한 동일 delivered population을 각각 인코딩한다. 두 번째 Poisson 생성이나 트랩 상태 복제는 없다. 획득 gain과 각 readout 경로의 analog/digital gain은 서로 다른 설정이다.

결과는 트랩 프로그램에서만 `camera_electrical_control_result_v2`이며, 실제 row event는 `camera_retained_row_events_v2`이다. 최종 `control_sequence`, `frame_conditions`, `planning_contract`, `prediction.sample_geometry` 및 `electrical_control` trace는 실제 RAW를 통해 결정된 설정을 기록한다. trace의 schema는 v1을 유지하며 v3 단일 경로도 `observation_readout_index=0`과 고정 observation 경로를 명시한다.

## 상태·시간·수치 계약

매 획득 시작의 free PD와 trapped occupancy는 0이다. 프레임 사이에는 각 행의 상태를 이어간다. 직전 실제 row transfer부터 다음 실제 row open까지가 gap이며, 처음 계획한 노출로 gap을 계산하지 않는다. gap 동안 photo admission은 없지만 dark generation과 내부 trap release/capture는 계속 계산된다. exposure 직전 clear는 free PD만 제거하며 trap occupancy는 보존한다. exposure 끝의 전송도 free population에만 적용한다.

같은 행의 시간 순서를 유지하면서 scene knot, PWM edge 및 최대 step 경계에서 분할한다. 각 구간의 광전하는 해당 실제 시간 범위의 irradiance basis와 PWM을 함께 적분한다. 전체 프레임 광전하를 uniform rate로 바꾸지 않으므로 같은 총 광량의 early/late PWM이 서로 다른 trap 상태와 RAW를 만들 수 있다. 행 간 계산 배치는 독립 상태에 대한 구현 방식이며 같은 행의 시간 순서를 바꾸지 않는다.

각 bounded step의 순서는 generation → PD overflow → release → PD overflow → capture이다. clear와 최종 transfer는 지정된 이벤트 위치에서 실행된다. deterministic mode는 실수 population을, stochastic mode는 Poisson 생성 및 binomial population split을 사용한다. trap capture의 finite vacancy 처리와 frozen-rate 단계는 **1차 근사 적분**이다. exact CTMC, exact SRH solution 또는 deterministic trajectory가 stochastic mean과 같다는 주장을 하지 않는다. capture/release 누계는 같은 전자의 재순환을 포함할 수 있으므로 unique electron 수가 아니다. 보존식은 시작·끝의 free/trapped population, 생성, overflow, clear drain 및 delivered charge를 함께 대조한다.

preflight는 initial exposure 하나가 아닌 도달 가능한 전체 노출 구간과 predecessor exposure에 대해 scene coverage, row overlap, gap dark, PWM/scene 경계, 최대 segment 수·pixel substep 수·작업 메모리·출력 byte를 제한한다. 초과하면 실행 전에 거절하며 조용히 step을 늘리지 않는다. 실제 획득 중에도 실제 시간 구간이 그 envelope 안에 있는지 확인한다. 광학 계산과 전하 동역학의 noise stream은 관측용 readout noise와 분리하며, 시퀀스를 prefix로 다시 계산해 RNG를 재사용하지 않는다.

수치 모델의 근거와 정확한 보존식·근사 한계는 [트랩 설계 검토](camera-platform-trap-design-review.md)를 참조한다. 일반적인 불완전 전송만으로 trap capture/release가 모델링됐다고 간주하지 않는다.

## RAW 기반 인과 제어

측광에는 프로그램이 고정한 경로의 원본 integer RAW와 ADC black level만 사용한다. 부호 있는 black-relative DN을 그대로 유지한다. trap occupancy, optical electron expectation, delivered-charge truth, 잔여 전하 또는 노출 역정규화로 관측을 보정하지 않는다.

명령은 모든 관련 row observation이 준비되고 선언된 지연이 지난 뒤 허용된 latch에서만 적용한다. 명령 생성, 전달, 만료·미적용 이유와 실제 적용 노출을 분리해 기록한다. 이 지연은 시뮬레이션에서 선언한 값이며 하드웨어 latency 측정값이 아니다. 관측 목표 도달 통계는 해당 시퀀스에서만 유효하고, 물리적 폐루프 안정성 또는 일반 PWM/LFM 강건성을 보증하지 않는다.

## 코드 영역 ISP 연결과 증거 검증

원본 RAW source adapter는 등록된 assembly, 모델, Test, scene, control, 프로그램 및 input artifact를 재검증한다. 저장된 summary JSON 바이트와 완료 결과의 equality, 모든 RAW 경로의 ADC 범위, 원본 container hash, 선택 경로 hash 및 출력 descriptor를 확인한다. 선택한 ISP 표시 경로가 LCG여도 AE 측광이 HCG였다면 HCG RAW로 controller trace를 재구성한다.

실제 RAW에서 순수 controller만 재실행해 적용 노출·명령·통계·latch를 복원하고, 원본 실제 row event 및 4개 기본 timing NPY와 대조한다. v3에서는 실제 설정과 immutable waveform/scene clock으로 trap 구간 schedule을 다시 만들고 다음 다섯 NPY의 shape/dtype/unit/bytes/hash/값을 대조한다: `trap_segment_start_ns`, `trap_segment_end_ns`, `trap_segment_duration_ns`, `trap_segment_phase`, `trap_segment_count`. 저장된 실제 schedule summary와 전체 노출 격자의 resource envelope도 대조한다. 이 과정은 시간 계획 검증이며 광자 생성·전하 동역학 재실행이나 trap truth 배열 로딩이 아니다.

ISP 처리 함수의 입력은 선택한 원본 F×H×W integer RAW, 실제 raw format, authored profile, geometry, 실제 frame records와 row events뿐이다. 프로그램·trace·trap ledger가 포함된 원본 source snapshot은 증거 보관용이며 처리 인자가 아니다. 비선형 코드를 선형 전하로 역변환하거나 dark subtraction·노출 정규화·trap 보상을 하지 않는다. 노출 설정이 바뀌면 기존 temporal filter의 settings reset이 작동한다.

replay와 comparison은 같은 원본 source와 실제 획득 정보를 보존한다. HCG/LCG 선택은 원본 shared acquisition identity를 유지한다. 원본 readout format, 정수 RAW 및 실현된 노출은 후보 profile을 바꾸어도 고정된다. 비교 수치는 code-domain 필터 간 수치 변화이며 물리적 화질 향상 또는 trap 보상 성능의 검증을 뜻하지 않는다. source discovery는 metadata-only이고, 원본 bytes 검증은 실제 선택·계획·실행·재조회에서 수행한다.

## 증거 상태

현재 증거 폴더는 `reports/camera_platform/trapped-control-v0161/`이다. baseline source archive는 개발 전 상태를 별도로 보존한다. ISP 연결은 새 실제 획득·재처리 테스트 6개와 기존 source/replay/paired/처리기 회귀 48개가 통과했다(`isp-initial.xml`, `isp-legacy.xml`). 네 전기 계열의 active trap RAW, PWM·비선형, 실제 노출 변경 시 temporal reset, 반대 HCG/LCG 표시 선택, replay·comparison·cache·force·ZIP·cold readback 및 재해시한 시간 증거 변조 거절을 포함한다. 동일한 현재 환경에서 보관된 dev8 adapter와 비교한 v1 single 및 v2 nonlinear HCG/LCG의 source 전체 JSON, 선택 RAW와 discovery 결과도 정확히 같았다(`isp-legacy-byte-parity.json`). 이 증거는 해당 계산·입력 경계의 검증이며 전체 제품 회귀나 물리 카메라 검증은 아니다. HTTP/Worker/browser/재시작 검증은 별도 실제 실행 증거가 생기기 전에는 완료로 간주하지 않는다. 설치된 기존 제품과 primary 상태를 이 개발 작업의 결과로 바꾸지 않는다.
