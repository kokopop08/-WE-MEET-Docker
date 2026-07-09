# WE-MEET 스케줄러 전면 개편 패치 노트

- **작성일**: 2026-07-09
- **범위**: 회수(Eviction) 메커니즘 · Q-Learning 상태/보상 · Static/Dynamic 스케줄러 · 학습 세계(Simulator) 통일
- **한 줄 요약**: "Static이 1등으로 나오던 역설"의 근본 원인(승패를 가르는 변수가 에이전트에게 보이지 않고, 학습 세계가 현실과 어긋나 학습 결과가 버려지던 문제)을 제거하고, 세 스케줄러가 각기 다른 철학을 관측 가능한 세계에서 겨루도록 재설계.

---

## 1. 배경: 무엇이 문제였나

벤치마크에서 가장 단순한 **Static 스케줄러가 1등**으로 나오는 역설이 발생했다. 기대했던 순위는 `dynamic ≳ q_learning > static` 이었으나 정반대였다. 원인을 코드까지 파고든 결과, 스케줄러 품질 문제가 아니라 **환경(세계)이 Dynamic·Q-Learning의 강점을 시험하지 못하게 물러진 것**이 근본 원인이었다.

### 진단된 근본 원인 4가지
1. **회수 동역학이 에이전트에게 안 보였다.** 승패를 실제로 가르는 변수 = `실행시간 / 회수폴링주기` 만큼의 회수 복리 노출인데, 이 정보가 Q-Learning의 상태·보상 어디에도 없었다. → 에이전트는 "싸고 느린 Spot-B"를 편애하도록 구조적으로 강제되어 Dynamic의 실수를 그대로 학습.
2. **지연 보상 훅이 죽어 있었다.** 완료 시점의 실제 성공/실패 기반 Q-업데이트가 존재했으나, Q-Learning이 실행 스레드를 `state=None, action=None`으로 호출해 `if state is not None` 가드에 걸려 **한 번도 실행되지 않았다.** 실제 학습된 것은 배정 시점의 낙관적(`success=True`) 보상뿐 → 회수가 나도 페널티가 Q값에 반영되지 않음.
3. **OOM·자원경합이 실질적으로 사라졌다.** 호스트 보호를 위해 PyTorch 부하를 낮추면서, 자원-무지(Static)를 처벌할 유일한 물리 메커니즘이 거의 발동하지 않게 됨.
4. **학습/추론 세계가 분열돼 있었다.** 학습 sim은 Spot-B도 회수도 없는 4-튜플 상태, 실제 docker는 4-튜플이지만 인코딩이 다름 → **상태 키가 안 맞아 사전학습된 Q-테이블이 추론에서 통째로 버려지고 있었다.**

### 설계 원칙
- **진실(Truth)은 실제 docker 경로(`head/`)**. 평가는 docker에서.
- **학습은 오프라인 sim에서** (docker로 50k 에피소드는 며칠~몇 주 소요 → 비현실적). 단, **sim이 docker 물리를 정확히 미러링**하여 sim-to-real 전이를 보장.

---

## 2. 변경 내역 (Phase별)

### Phase 0 — 회수(Eviction) 메커니즘 단일화
- **`common/failure_simulator.py`**
  - 회수 확률의 유일 진실을 `cost_model.yaml`의 `preemption_probability`(spot_a 0.30 / spot_b 0.10)로 확정. `EVICTION_BASE_PROB`는 값을 일치시킨 폴백으로 강등.
  - `current_danger_phase(now)` 헬퍼 신설: 30초 사이클 중 앞 10초가 위험구간(1). 시간 결정적이라 데몬과 스케줄러가 같은 값을 관측.
  - `eviction_probability()` 헬퍼로 판정·로그가 동일 확률을 사용하도록 일원화.
- **`head/cluster_manager.py`**
  - 회수 데몬이 위 헬퍼를 사용. **로그 표시값 ≠ 실제 확률** 버그 수정(과거 로그는 0.50을 찍고 실제로는 0.30을 적용).

### Phase 1 — Q-Learning 상태 특징 단일화 + 6-튜플 확장
- **`head/q_learning/state_features.py` (신규)**
  - `compute_state(...)` 순수 함수 + `compute_current_state(gcs_state)` 를 유일 상태 산출 함수로 도입.
  - 기존에 3곳(스케줄러 현재상태 / 다음상태 / 완료 피드백 next_state)에 복붙돼 있던 상태 계산을 전부 이 함수로 위임 → 드리프트 차단.
  - **새 상태 튜플(6차원)**: `(q_bucket, head_model, a_mix, sla_bucket, danger_phase, budget_level)`
    - `q_bucket`(0~3): 대기열 적체 깊이 — **캐스케이드 신호(신규)**
    - `head_model`(0/1): 선두 태스크 성격(연산바운드 CNN / 메모리바운드 RNN·LSTM) — OOM 라우팅
    - `a_mix`(0~7): OD/Spot-A/Spot-B IDLE 비트맵
    - `sla_bucket`(0~2): 마감 완급(여유>30s / 중10-30s / 임박≤10s)
    - `danger_phase`(0/1): 스팟 회수 위험구간 — **룰렛 타이밍 신호(신규)**
    - `budget_level`(0~2): 잔여 예산(위험<0.7 / 낮음<3.0 / 여유)
- **`head/scheduler/q_learning.py`**: 메인 루프의 상태 계산을 `compute_current_state`로 교체, `_calculate_next_state`도 위임으로 축소.

### Phase 2 — 보상 재설계 (지연 보상 활성화 + 회수 페널티)
- **`head/scheduler/q_learning.py`**
  - 배정 시점의 낙관적 즉시 보상(`_calculate_immediate_reward`) **제거**.
  - 실행 스레드 호출을 `run_task_on_worker(..., None, None)` → **`..., state, action`** 으로 변경 → 완료 시점 지연 보상 경로 활성화.
  - HOLD 보상을 `1.0 - 0.5·큐길이 - 마감초과페널티` 로 변경(무행동 함정 방지).
- **`head/scheduler/task_executor.py`**
  - 완료 `finally`에서 **회수 vs OOM 구분**: 완료 시점에 워커가 레지스트리에서 사라졌으면 회수(Eviction)로 판정.
  - 완료 시점 Q-업데이트가 실제 성공/실패와 `evicted` 플래그로 수행되도록 연결. next_state도 `compute_current_state`로 위임.
- **`head/q_learning/agent.py`**
  - `calculate_reward`에 `evicted` 인자 추가.
  - `SUCCESS_REWARD` 10 → **20** (정상 배정이 확실히 양의 기대값이 되도록; 무행동 함정 방지).
  - `EVICTION_PENALTY = 25` 신설(회수 실패 시 강한 벌점).
  - `MAKESPAN_WEIGHT = 0.5` 신설(마감 초과와 무관하게 '느림' 자체를 상시 감점 → "싸지만 느린" Spot-B 편향 제거).

### Phase 3 — OOM·자원경합을 "동거 압력 확률"로 모델링
- **`common/failure_simulator.py`**
  - `check_oom(model_type, task_id, node_type, co_membound_count)` 로 확장(하위호환: node_type=None이면 레거시 8% 유지).
  - 노드 용량 인지 확률: LSTM on `spot_b` 0.20 / `spot_a` 0.08 / `on_demand` 0.02, RNN은 훨씬 낮게. 동거 메모리바운드 1개당 +0.08(상한 0.60). CNN 등 연산바운드는 0.
- **`head/scheduler/task_executor.py`**: 배정 직전 head 측에서 OOM 해저드를 굴려(동거 정보가 보이는 지점) 실패 시 재큐잉. 실제 호스트 메모리는 건드리지 않음(주사위 모델) → 자원을 안 보고 작은 노드에 큰 모형을 얹는 스케줄러가 처벌받음.

### Phase 4 — Static / Dynamic 스케줄러 재설계
- **`head/scheduler/static.py`**: "속도 최우선 / 비용·자원 무시" 극단 베이스라인. OD 최우선 → Spot-A만(Spot-B 배제), 자원 상태 무시, 공격적 증설(큐≥2 → +1, ≥6 → +2).
- **`head/scheduler/dynamic.py`**: Task-Aware 매칭(`MODEL_NODE_PREFERENCE`). LSTM→OD/Spot-A(Spot-B 금지, OOM 회피), CNN→Spot-A 우선, RNN→Spot-B 우선. 증설 타입도 큐 구성에 맞춰 선택(LSTM/CNN 있으면 Spot-A, RNN 위주면 Spot-B). 기존 간섭 회피(과부하 노드 배제)·백필링 유지.
- **`head/state.py`**: `INITIAL_VIRTUAL_BUDGET = 1.5` 상수 도입(버스트 시나리오에서 예산 축이 실제로 물게). `head/dashboard/server.py`의 /reset도 이 상수 사용.

### Phase 5 — 학습 세계 통일(Simulator 미러링) + 재학습
- **`head/q_learning/pretrain.py` (통합 재작성)**
  - 실제 docker 물리를 미러링하는 `SimulatedEnvironment` + `train_offline`을 이 단일 파일에 통합(과거 `scratch/train_simulation.py`의 로직 흡수).
  - 미러링 요소: 노드 3종·gpu_scale·요금 / 노출시간 기반 회수(10초 폴링 + 30초 위험구간) / OOM 동거모델 / 6액션·6튜플 상태(`compute_state` 공유) / ASSIGN의 지연 보상 크레딧 할당.
  - 이 sim의 산출물 `data/q_table.json`이 곧 **warm-start** Q-테이블.
- **삭제된 파일**
  - `scratch/train_simulation.py` — pretrain.py로 통합.
  - `scratch/run_benchmark.py` — 스케줄러를 가짜로 재구현하고 물리도 현실과 달라 신뢰 불가 → 은퇴. 평가는 docker의 `log_benchmark_metric`로 일원화.
  - `scratch/benchmark_report.txt` — 삭제된 run_benchmark의 고아 출력.
- **유지된 파일**
  - `scratch/visualize_benchmarks.py` — 실제 docker 벤치마크 CSV(`data/benchmark_results_<mode>.csv`)를 읽어 6패널 비교 차트를 생성(평가 진실과 짝).

---

## 3. 검증 결과 (순수 파이썬 sim, 50,000 에피소드)

- **상태 인코딩**: Q-테이블 키가 전부 6-튜플 arity로 정상 저장(과거엔 추론과 키가 안 맞아 버려지던 문제 해결). 학습 상태 수 ≈ 600.
- **보상 방향성**: 성공 배정 +13.9 vs 회수 실패 −29.1 → 회수가 확실히 더 나쁨.
- **예산 생존율 ≈ 66%**: 초기 예산을 낮춰(0.5~3.0) 예산 고갈 동역학을 실제로 겪게 함.
- **학습된 정책(마스킹 적용, OD busy 상황에서 A vs B)**
  - 예산 축: 낮음 → Spot-A, **위험 → Spot-B**(파산 회피, 최저가로 전환). ✅
  - 긴급(HOLD 마스킹) → Spot-A(빠름)로 배정. ✅
  - OOM 라우팅: 메모리바운드 → OD/Spot-A 선호, Spot-B 최하위. ✅
  - 회수 타이밍: danger → Spot-B 회피 신호 **존재하나 약함**(γ=0.9 장기수익에 −25 페널티가 희석). ⚠️

---

## 4. 남은 일 / 알려진 한계

- **실제 docker 벤치마크 미실행**: 개발 환경에 torch/docker 부재로 순수 파이썬 sim 검증까지만 수행. `SCHEDULER_MODE`을 static→dynamic→q_learning으로 각각 구동 후 `scratch/visualize_benchmarks.py`로 비교 필요. 기대: **static(빠르나 파산) < dynamic(견고) < q_learning(균형)**.
- **회수-타이밍 가중치 튜닝**: sim으로 완전히 확정 못 한 유일한 축. 실제 docker 결과를 보고 `EVICTION_PENALTY`(현 25) / `gamma`(0.9) 균형 조정.
- **예산 여유(budget_level=2) 상태 과소 탐색**: 시나리오 예산 $1.5가 budget_level 1에서 시작하므로 실제 운영엔 지장 없으나, 예산을 크게 잡는 다른 시나리오에선 재학습 권장.
- **초기 예산 $1.5는 전역 기본값**: 일반 구동에서도 수 분이면 예산이 마름(의도된 시나리오 값). 필요 시 `head/state.py::INITIAL_VIRTUAL_BUDGET` 상향.

---

## 5. 백업 / 롤백

- 구 4-튜플 Q-테이블은 `data/q_table.json.bak_old4tuple`에 백업.
- 재학습 방법: `python head/q_learning/pretrain.py` (기존 `data/q_table.json` 삭제 후 클린 학습 권장).

---

## 6. 수정/삭제 파일 요약

| 파일 | 변경 |
|---|---|
| `common/failure_simulator.py` | 회수 확률 단일화, `current_danger_phase`/`eviction_probability`, `check_oom` 동거모델 확장 |
| `head/cluster_manager.py` | 회수 로그 실제값, danger_phase 공유 헬퍼 사용 |
| `head/q_learning/state_features.py` | **신규** — 6-튜플 상태 단일 산출 함수 |
| `head/q_learning/agent.py` | `evicted` 인자, `EVICTION_PENALTY`/`MAKESPAN_WEIGHT`, `SUCCESS_REWARD` 20 |
| `head/q_learning/pretrain.py` | **재작성** — 세계 미러링 sim 통합, 단일 학습 진입점 |
| `head/scheduler/q_learning.py` | 즉시 보상 제거, 실제 state/action 전달, 6-튜플 상태·마스킹, HOLD 보상 |
| `head/scheduler/task_executor.py` | 지연 보상 활성화(회수/OOM 구분·페널티), OOM 사전판정, next_state 위임 |
| `head/scheduler/static.py` | 속도 최우선/OD 즉시 배정 베이스라인 |
| `head/scheduler/dynamic.py` | Task-Aware 노드 매칭 |
| `head/state.py` | `INITIAL_VIRTUAL_BUDGET = 1.5` |
| `head/dashboard/server.py` | /reset이 `INITIAL_VIRTUAL_BUDGET` 사용 |
| `scratch/train_simulation.py` | **삭제**(pretrain으로 통합) |
| `scratch/run_benchmark.py` | **삭제**(은퇴) |
| `scratch/benchmark_report.txt` | **삭제**(고아 출력) |
| `scratch/visualize_benchmarks.py` | 유지(docker 벤치 CSV 시각화) |
