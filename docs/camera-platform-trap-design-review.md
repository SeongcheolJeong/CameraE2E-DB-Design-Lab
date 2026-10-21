# Native PD trap capture/release 설계 검토

검토일: 2026-09-24. 상태: 원논문에 근거한 **authored behavioral model 계약 제안**. 특정 CIS 공정, trap 에너지, 밀도, 온도 의존성 또는 실측 image lag를 검증한 모델이 아니다. 이 문서 작성 과정에서는 설치 환경·기존 증거·제품 소스를 변경하지 않았다. 이후 내부 구현 범위는 아래의 좁은 1-species 근사 계약으로 선택했다. 이는 사용자 또는 실물 소자의 물리 검증 승인을 뜻하지 않는다.

핵심 권고는 native pixel마다 자유 PD 전하 `Q`와 유한 trap 점유량 `T`를 별도로 보존하고, **실제 시간 순서의 광 생성 구간**을 처리하는 것이다. 프레임 전체 광전하를 노출 시간으로 나눈 평균 rate를 넣어서는 PWM/LFM의 early/late 순서를 재현할 수 없다. 기존 불완전 전송 계수 `eta`와 trap occupancy는 서로 다른 원인이므로 이름과 증거를 합치지 않는다.

## 문헌으로 확인한 범위

- Shockley–Read의 원논문은 서로 한 전자 전하만큼 다른 두 점유 상태를 갖는 trapping center로 재결합 통계를 설명한다. 이것은 bounded occupancy의 물리적 동기이며, 아래의 공간 평균 PD 식이 그 논문의 완전한 SRH/소자 모델이라는 뜻은 아니다. [Shockley & Read, Physical Review 87, 835–842 (1952)](https://journals.aps.org/pr/abstract/10.1103/PhysRev.87.835).
- Fowler–Liu는 4T CIS 전송 중 Si–SiO₂ 계면의 포획·방출을 전송 잡음의 중요한 후보로 다루고 random telegraph 모델을 제안한다. 따라서 **노출·gap 동안 항상 PD와 교환하는 이번 모델을 실제 TG 계면 trap 모델이라고 부르면 안 된다**. TG 전압·전송 지속시간·FD 상태는 별도 확장이다. [Fowler & Liu, Charge Transfer Noise in Image Sensors, IISW 2007](https://imagesensors.org/papers/10.60928/1hxk-uyad/).
- Bonjour 등의 측정 연구는 potential barrier/pocket, gate traps, spill-back 원인을 분리하려는 방법을 제시한다. 단일 bright→dark tail만으로 `eta`, trap capacity, capture rate, release time을 모두 고유하게 식별했다고 주장할 수 없다. [Bonjour, Blanc & Kayal, Experimental Analysis of Lag Sources in Pinned Photodiodes, IEEE EDL 33, 1735–1737 (2012), EPFL 원저자 저장소 설명](https://graphsearch.epfl.ch/fr/publication/184055), [DOI](https://doi.org/10.1109/LED.2012.2217474).
- 실제 PPD→FD transfer는 thermionic barrier, transfer 시간, FD reset voltage 등의 영향을 받는 별도 문제다. 이번 즉시 `eta` split은 그 장치 물리의 해결을 의미하지 않는다. [Han, Yao & Theuwissen, A Charge Transfer Model for CMOS Image Sensors, IEEE TED 63, 32–41 (2016), TU Delft 원저자 저장소](https://research.tudelft.nl/en/publications/a-charge-transfer-model-for-cmos-image-sensors/).

위 자료에서 기구 구분과 모델 한계를 가져왔다. 아래 보존식·API·수치 방식·시험 조건은 이 플랫폼을 위한 설계 제안이다. 원문 접근이 제한된 항목은 공개 초록/저장소 설명까지만 확인했으며, 비공개 전문을 읽었다고 전제하지 않는다.

## 상태와 보존식

한 native site에서 `0 <= Q <= W`, 종 `j`에 대해 `0 <= T_j <= N_j`를 유지한다. `W`는 자유 PD capacity, `N_j`는 해당 종의 한 전자 trap 수에 해당하는 authored capacity다. 첫 버전은 **1종**, 이후 최대 2종으로 제한할 수 있다. `Q,T,N,W`는 electron-count 좌표이고 stochastic 모드는 정수다. deterministic 모드는 연속 실수 근사다. 4개 RGGB site별 parameter가 다를 수 있지만, binning·대표 격자 대신 모든 native site에 상태를 둔다.

제안된 질량 작용 식은 다음과 같다. `g(t)`는 PRNU를 한 번 적용한 admitted photo rate와 dark rate의 합이다.

```text
C_j(Q,T_j) = k_j * Q * (1 - T_j/N_j)     [electron/s]
R_j(T_j)   = T_j/tau_j                   [electron/s]
dQ/dt     = g(t) - sum(C_j) + sum(R_j) - spill_rate
dT_j/dt   = C_j - R_j
```

`k_j`의 단위는 `1/s`다. `c_n*n*(N-T)` 같은 carrier-density 식과 연결하려면 유효 접근 부피, capture cross-section, thermal velocity가 필요하다. 이 계약은 그 값을 추정하지 않고 `k`를 작성 입력으로 받는다. 특히 `k`를 고정한 채 `N`만 바꾸는 행위는 물리적 defect 밀도 변화와 동일하지 않다. `N=0`은 식의 분모 때문에 별도 bypass로 정의하거나 거절해야 한다. 선택된 초기 schema는 양의 `N`과 `k=0` 비활성 경계를 사용한다.

하나의 구간/프레임에서 다음 식을 array별로 검사한다.

```text
Q_in + sum(T_in) + generated_photo + generated_dark
  = Q_out + sum(T_out) + delivered_to_read_node
    + pd_clear_loss + overflow_loss
```

Capture는 `Q`에서 뺀 바로 그 양을 `T`에 더하고, release는 `T`에서 뺀 바로 그 양을 `Q`에 더한다. 둘을 다시 Poisson 생성하지 않는다. PD capacity와 trap capacity는 별개다. 방출로 PD가 넘치면 overflow로 기록하며, 증거에서 사라지게 하면 안 된다. ADC saturation/readout noise는 이 전하 보존 ledger의 sink가 아니다.

이벤트는 기존 같은-row 시계에 맞춰 다음과 같이 정의한다.

- 노출이 닫혀도 내부 release와 dark 생성은 계속된다. 닫힌 photo admission gate를 trap 전하를 버리는 drain으로 해석하지 않는다.
- `PD clear`는 자유 `Q`만 제거하고 `T`는 유지한다. trap reset이 필요하다면 별도 물리 계약과 별도 loss ledger가 필요하다.
- transfer 시 `D=eta*Q` 또는 `D~Binomial(Q,eta)`이며 `Q←Q-D`, `T`는 유지한다. 같은 delivered `D`를 HCG/LCG encode-only 경로가 공유한다.
- read-node reset은 PD/trap clear가 아니다. 동일 timestamp에서 기존 `transfer → clear → open` 순서를 보존한다.
- row별 첫 노출 시작에서 `Q=T=0`; 그 이전 dark/release/preconditioning은 가정하지 않는다. 초기 점유 상태·대기 이력은 향후 별도 입력이다.

Gap이 길어지면 release가 늘지만, 뒤따르는 clear가 그 자유 전하를 제거할 수 있다. 따라서 “gap 증가 ⇒ 다음 RAW 증가”를 보편 시험으로 사용하지 않는다. clear 유무·transfer 시점을 함께 비교해야 한다. release 후 같은 구간에서 다시 capture될 수 있는 **재포획**도 명시한다.

## 시간 입력: 프레임 평균 금지

기존 `retained_charge.step`은 프레임별 합산 photo mean을 받는다. trap kinetics에서는 충분하지 않다. 같은 적분 photon 수라도 early pulse는 release/recapture에 쓸 시간이 더 길고, late pulse는 그렇지 않다. whole-period exposure에서도 trap 때문에 PWM phase가 영향을 줄 수 있다. 이것은 trap 없는 선형 적분기의 whole-period phase invariance와 다르다.

각 row의 구간은 실제 노출/전송/clear 시각, scene knot, PWM ON/OFF edge, 허용 최대 시간 간격에서 나눈다. 구간은 `[start_ns,end_ns)`이며 모든 절단 시각은 정수 ns다. 음수 phase origin, rolling row offset, exact end boundary를 포함한다. period가 작다고 무한 edge 목록을 만든 후 예산을 확인하지 말고, 개수를 먼저 계산하여 거절한다.

광학 계산은 scene knot의 native rate를 재사용할 수 있다. 각 짧은 구간에서 기존 선형 scene basis × PWM의 해석적 적분으로 photo mean을 계산한다. 단, 구간 내부를 일정 rate로 취급하는 분할 방법은 여전히 시간 근사다. waveform 불연속은 정확히 자르고, 선형 변화는 `dt/2` refinement로 확인한다. **프레임 총량만 같은 입력으로 대체하지 않는다.** 외부 시계가 다른 row는 상태 교환이 없으므로 벡터화할 수 있지만, 각 row의 시간 cursor는 독립적으로 전진해야 한다.

## 수치 방법의 선택과 한계

| 방식 | 장점 | 반드시 구분할 한계 |
|---|---|---|
| 현재 구현 선택: generation → PD overflow → release → PD overflow → capture, 작은 bounded step | native vectorization, 비음수·capacity·ledger 보존, 정수 stochastic split | 1차 연산분할; 구간 안 사건 순서가 근사이며 exact CTMC 아님 |
| 정확한 단독 capture/release subflow + 대칭 분할 | deterministic positivity/보존에 유리; 매끈한 비포화 구간에서 2차 설계 가능 | 전체 결합 문제는 여전히 분할 오차; overflow/clear 불연속은 차수를 낮출 수 있음 |
| 전체 CTMC의 Gillespie/SSA | 선언한 Markov jump model에 대한 사건 시간·분포 기준 | 고광량에서는 generation 사건 수가 너무 큼; 작은 상태 reference에 적합 |

연산분할의 고차화는 작은 시간 간격의 상호 연산을 정확하게 조합하는 문제다. 이름만 Strang이라고 붙여서 2차를 주장하지 말고, 실제 subflow·시간 의존 generation·capacity 경계를 포함해 수렴률을 측정해야 한다. [Strang, On the Construction and Comparison of Difference Schemes, SIAM JNA 5, 506–517 (1968)](https://epubs.siam.org/doi/10.1137/0705041).

선택된 빠른 방식의 한 step `h`에서 release는 `p_r=-expm1(-h/tau)`, `released~Binomial(T,p_r)` 또는 deterministic `T*p_r`다. 그 후 vacancy를 갱신하여 capture 확률을 계산한다. 자유 전자별 `p_c=-expm1(-k*(1-T/N)*h)`를 사용한 binomial과 vacancy 제한은 보존·capacity를 지키지만, 같은 step 안의 vacancy depletion을 정확히 풀지는 않는다. `min(sample,N-T)`는 선택된 근사법의 일부로 공개해야 한다.

확률 상한 `p<=0.02`만으로 오차 보장이 완성되지 않는다. 자유 전자 hazard `k*(1-T/N)`뿐 아니라 **빈 trap 하나의 fill hazard `k*Q/N`**도 검사해야 한다. 보수적인 사전 bound는 `max(k,1/tau,k*W/N)`이다. 정수 ns에서 가능한 step이 1 ns보다 작으면 silently clamp하지 말고 거절한다. 입력 generation rate·overflow·최대 step 수·pixel-step work·메모리도 queue 전에 확인한다.

Deterministic reference를 위한 정확한 단독 capture 식도 마련할 수 있다. `v=N-T`, `a=k/N`, `u=min(Q,v)`, `d=abs(Q-v)`라 두고 `phi=-expm1(-a*d*h)/d` (`d=0`이면 `a*h`)이면 capture-only 이동량은 다음과 같다.

```text
captured = u*(d+u)*phi/(1+u*phi)
Q' = Q-captured
T' = T+captured
```

이는 생성·release·overflow가 없는 단독 반응식에서 직접 도출한 기준이다. release-only는 지수식으로 정확하다. 두 종에서는 공유 `Q`를 독립 복사해 동시에 capture하면 이중 소비가 생긴다. 종별 보존 exchange를 순차 적용하고, 순서 역전 및 step refinement를 검사한다. 대칭 순서로 개선할 수 있으나 처음에는 1종만 검증하는 편이 분명하다.

Stochastic exact reference는 reaction propensity `g`, `k*Q*(1-T/N)`, `T/tau`와 `(+1,0)`, `(-1,+1)`, `(+1,-1)` jump를 갖는 CTMC다. 외부 PWM/clear/transfer 시각에서 경로를 정확히 끊는다. [Gillespie, Exact stochastic simulation of coupled chemical reactions, J. Phys. Chem. 81, 2340–2361 (1977)](https://pubs.acs.org/doi/abs/10.1021/j100540a008), [원논문 전문, University of Cambridge 보관본](https://www.cl.cam.ac.uk/teaching/2425/Bioinfo/papers/DanielGillespie1.pdf).

생성·spill 없는 작은 닫힌 계에서 `M=Q+T`라면 trap 점유의 birth rate는 `k*(M-t)*(1-t/N)`, death rate는 `t/tau`다. 유한 generator matrix exponential로 분포/평균/분산 기준을 독립 계산할 수 있다. Poisson generation 적분 자체가 정확해도 그 전자들을 전부 substep 앞쪽에 놓는 결합 방식은 시간 근사다. 비선형 `E[Q*T]` 때문에 finite stochastic ensemble mean을 deterministic ODE와 무조건 동일시하지 않는다.

## 구현 계약 초안과 선택된 좁은 범위

첫 slice는 `camera_trapped_photodiode_v1`, 1종만 지원한다. `capacity_e`, native R/Gr/Gb/B의 `transfer_efficiency[4]`, 양의 integer `trap_capacity_e[4]`, `capture_rate_s[4]`, 양의 `release_time_s[4]`, `integration={method:'bounded_split_step_v1',max_step_ns,max_transition_probability:0.02}`를 exact keys로 받는다. 상수들은 작성 예제이며 제조사 수치가 아니다. 온도에 따른 `k/tau` 법칙은 이 버전에 없다.

새 `camera_retained_charge_capture_program_v2`만 이 모델을 받으며 plugin은 기존 `retained_charge_v1`을 재사용한다. v1 프로그램·core·RNG stream·결과 bytes는 그대로 둔다. v2 event/result schema를 구분한다. 현재 electrical AE 프로그램이 trap 상태를 조용히 버리며 실행되지 않도록, 그 경로는 새 trap 계약을 지원하기 전까지 명시적으로 거절한다.

Runtime→core segment는 `phase:'gap'|'exposure'`, `duration_ns:H integer vector`, `expected_photo_e:H×W`다. gap들을 먼저 처리한 후 row별 clear를 한 번 적용하고 exposure 구간을 처리한 뒤 transfer를 한 번 한다. row의 0-duration은 0 photo를 요구한다. raw 입력용 광전하는 각 짧은 시간 구간에서 계산하며 PRNU·Poisson·dark·well clipping·ADC는 각각 맡은 계층에서 한 번만 수행한다.

결과에는 free/trapped 초기·최종 값, generated/captured/released/clear/overflow/delivered ledger, row별 elapsed ns, segment 일정 hash, 계산 step·work·수렴 방식, readout descriptor를 남긴다. trap ledger는 diagnostics이고 AE/ISP의 관측치나 역보정 입력이 아니다. DCG 양 경로는 같은 delivered population을 소비한다.

## 필수 검증과 후속 측정

1. 모든 step과 전체 프레임에서 보존·비음수·두 capacity를 확인한다. stochastic 모드는 정수 exact equality, deterministic 모드는 선언된 수치 오차를 사용한다.
2. release-only 지수식과 binomial 분포, capture-only 정확 해, 작은 닫힌 CTMC matrix exponential을 독립 reference로 사용한다. 테스트가 제품 helper를 expected 계산에 재사용하지 않게 한다.
3. 일반 결합 ODE를 별도 adaptive solver로 풀어 deterministic step/2, step/4 수렴을 확인한다. 정확한 시간 경계가 있는 테스트와 capacity 활성 테스트를 구분한다. step bound는 물리 오차 보증이 아니다.
4. 같은 total photon의 early/late PWM에서 occupancy와 실제 RAW가 달라지는 반례를 검증한다. phase만 바꾸고 photo integral은 같게 유지한다. trapping-disabled 경계의 기존 waveform 적분도 확인한다.
5. bright→dark tail, no-light release, long gap+clear/no-clear, zero gap, coincident transfer/clear, rolling row offset, saturation 후 release overflow, 4-site parameter 차이를 확인한다.
6. 모든 readout family와 HCG/LCG shared delivered population, nonlinear response 원형 보존, v1 전체 결과/RNG tail 동등성, malformed state·elapsed time·budget 거절을 확인한다.
7. 물리 calibration은 여러 조도·pulse width·transfer delay·TG timing·dark wait·온도와 clear 정책의 측정 세트가 있어야 한다. 현재는 그 자료가 없으므로 `source_kind=authored/simulated`, `physical_validation=not_established`, parameter uncertainty unknown을 유지한다.

권고 결론은 **작은 1종, 보존 가능한 근사 integrator, 시간 순서를 보존하는 runtime, 명시적 수렴 증거**부터 제공하는 것이다. 이는 일반 SRH/TCAD나 실측 CIS image lag 검증을 완료하는 단계가 아니다.
