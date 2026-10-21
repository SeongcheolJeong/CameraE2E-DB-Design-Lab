# 고정 노출 · 전기 RAW AE 응답 비교

개발 소스 `0.16.1.dev4`의 제한된 실험 기능이다. 설치된 기본 앱의 제공 버전과
독립적인 개발 검증 기록은 `reports/camera_platform/development-handoff.json`을 따른다.
원문의 일곱 모듈과 네 실행 방식을 유지하며, 기존 `electrical_control_v1` 캡처를
Experiment & Optimization의 비교 작업으로 연결한다.

## 가능한 실험

하나의 고정 카메라·장면·초기 제어·전기 AE 프로그램에서 정책 `enabled`만 다른
고정 노출/AE 쌍을 실제로 획득한다. 두 실행은 별도의 빈 PD와 난수 세션으로 시작한다.
실제 적용 노출이 장면/PWM 적분, 잔류 전하, 전기 readout, 정수 RAW를 바꾼다.

- 단일 선형 또는 비선형 readout, full-native RGGB, 최대 16프레임.
- 최대 두 축: PWM 위상, controller 지연, actuator 지연 중 선택.
- 조건 × 명시적인 seed 목록은 최대 12쌍, 실제 캡처는 최대 24개.
- noise를 끈 결정론 실행과 seed별 확률 실행. 동일 seed가 다른 노출의 픽셀별
  잡음 표본 일치를 보장하지 않는다.
- 동일 프레임 ID의 원본 RAW/PNG, 실제 적용 노출·관측 준비 시각·명령 이력과
  AE−고정의 코드/rail/시각 차이. 조건과 seed별 결과를 별도로 유지한다.

작업 화면: **실험 → 전기 AE · PWM 비교**. 등록된 네 입력을 선택하고 비교 초안을
생성한 뒤, JSON에서 축·seed·평가 구간을 정한다. 계획 검사는 기록을 쓰지 않으며,
전체 실행 수와 배열·미리보기·summary·ZIP 상한을 계산한다. 입력을 수정하면 계획을
다시 검사해야 한다. 확인된 hash로 제출하며, 불확실한 제출 응답을 자동 재시도하지 않는다.

## 관측과 판정

평가기에는 원본 integer RAW, 전기 RAW 형식, 실제 행별 노출과 관측 준비 시각,
고정된 평가 명세만 전달한다. 숨은 photon/PD ledger나 장면 truth는 전달하지 않는다.
ROI와 native R/Gr/Gb/B 사이트의 signed RAW−black DN을 정수 합으로 계산한다.
음수와 digital rail 표본을 유지한다. 노출로 나누거나 비선형 곡선을 역산하지 않는다.

평가 명세는 clock, 시작/끝, 작성자가 선언한 변화 시각, 순서 있는 frame ID 목록,
목표 DN 구간, 연속 관측 수, 최소 관측 span, 경계 노출 포함 규칙을 동결한다.
정상광 step 선언에는 변화 이후 평가 끝까지 원본 장면이 일정한지 확인한다.
실측 step 검출이나 물리적인 불연속 발생 확인은 아니다.

| 판정 | 의미 |
|---|---|
| `complete` | 유한한 관측열에서 첫 유지 조건을 확인함. 진입과 확인 시각을 구분함 |
| `not_reached` | 선언 관측을 충분히 확보했지만 목표 유지 조건에 미도달 |
| `censored` | 누락·경계 제외 또는 부족한 관측 수/span으로 확인 불가 |
| `not_applicable` | PWM에는 정상광 settling 정의를 적용하지 않음 |

관측 사이를 보간해 연속 안정성을 주장하지 않는다. 이후 목표 이탈도 그대로 남긴다.
Clipped 코드가 코드 목표를 만족하더라도 rail 비율을 숨기지 않는다. 단일 LFM 점수,
자동 승자, 모델 자동 승격, 물리 PASS는 제공하지 않는다.

## 실행·보존 계약

`camera_electrical_response_study_v1` 명세는 immutable Assembly/Scene/Control/Program,
모델·이미지·Test·프로그램 blob·환경과 자원 상한을 plan hash에 묶는다. 각 변형의
Capture Program/Test와 모든 child/parent 작업을 하나의 DB transaction으로 등록한다.
원래 기록은 수정하지 않는다. 소유권과 child request hash를 검증한다.

워커는 일반 캡처 child를 실행한 뒤 모두 terminal일 때 aggregate를 실행한다.
완료된 쌍과 실패/취소된 쌍을 함께 보존한다. 일부 쌍만 성공하면 `partial`, 성공한 쌍이
없으면 실패 상태와 `no_complete_pairs` 근거를 남긴다. 취소는 해당 부모와 그 부모가
소유한 미완료 child에만 적용한다. 재시작으로 중단된 child도 실패 근거로 유지한다.

조회와 cache 재사용은 원본 RAW·형식·실제 시간 연결을 검증하고 평가를 다시 계산한다.
전용 JSON/ZIP은 계획, 입력 closure, 모든 child 요청/결과, RAW·PNG·명령·평가 hash를
포함한다. ZIP의 생성 시각이 달라질 수 있어 재시작 동일성은 member 경로와 bytes로
판정한다. 전체 ZIP container hash 자체를 안정된 식별자로 사용하지 않는다.

API prefix는 `/api/v1/electrical-response`이다. `GET /template`, `POST /studies/plan`,
`POST /studies` (`specification`, `plan_hash`), `GET /studies`, `GET /studies/{id}`,
`POST /studies/{id}/cancel`, `GET /studies/{id}/export`, `GET /studies/{id}/bundle`을 제공한다.
SDK의 `electrical_response_template`, `plan_electrical_response`,
`submit_electrical_response`, `electrical_response`, `cancel_electrical_response`,
`export_electrical_response`, `electrical_response_bundle`이 같은 경로를 사용한다.

## 한계와 다음 우선순위

독립 실물 측정은 0건이며 모델 오차와 총 불확실도는 미확정이다. 일반 정적 실측
비교기의 closed-loop 거부 조건은 유지된다. 이번 결과로 물리 AE 안정성이나 상용
HDR/LFM 성능을 인증하지 않는다.

다음 확장은 고정 HCG/LCG 관측의 AE, 실제 AE RAW를 받는 code-domain ISP,
독립 reference를 둔 ISP/정책 비교다. 상태 이력을 포함하는 HDR fusion에는 별도
신호 모델이 필요하다. 기존 HDR의 독립 reset 가정을 잔류 전하 경로에 재사용하지 않는다.
유한 물체 거리·깊이의 광학, 픽셀/전하 파라미터 식별, trap/release·CDS 등 일반 모델의
확장도 사용자 우선순위에 남아 있다.
