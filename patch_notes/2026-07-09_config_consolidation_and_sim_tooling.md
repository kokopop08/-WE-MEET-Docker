# WE-MEET 설정 단일화 & 측정 도구 강화 패치 노트

- **작성일**: 2026-07-09
- **범위**: 확률/환경 변수 단일 파일화(`sim_env.yaml` + `env_config.py`) · `run_simulations.py` 발표용 측정 도구 강화 · 기능별 리팩토링 설계 확정(이동은 발표 후)
- **한 줄 요약**: 4곳에 복붙되어 드리프트를 반복하던 "확률적 변수"를 단일 YAML의 유일 진실로 모으고(값 무변경), 임시 벤치마크 도구를 통계·지표·시나리오·리포트를 갖춘 발표용 측정기로 승격.

---

## 1. 배경: 무엇이 어려웠나 (difficulties/risks)

파일이 많아지고 복잡해지면서 코드 수정이 위험해졌다. 특히 **동일한 물리 상수가 세 개의 서로 다른 "세계"에 복붙**되어 있었다:
- 실제 docker 경로(`head/scheduler/*`, `head/q_learning/*`)
- 오프라인 학습 sim(`head/q_learning/pretrain.py`)
- 고속 벤치마크 sim(`scratch/run_simulations.py`)

과거 패치들이 `state_features.py`(상태 인코딩)·`reward_policy.py`(비-ASSIGN 보상)·`failure_simulator.py`(장애 확률)로 **로직**의 유일 진실은 세웠지만, **수치(데이터)** 는 여전히 흩어져 있었다. 예: 실행시간 캘리브레이션(`EXEC_FLOOR 1.5`, `PER_EPOCH`), 시간당 요금(`7.10/2.20/0.90`), 태스크 생성확률, `MAX_TASK_ATTEMPTS`, 버킷 임계 등이 2~3곳에 중복. 한쪽만 바꾸면 조용히 어긋나 "학습한 물리 ≠ 평가한 물리"가 재발할 위험이 상존했다.

동시에 실제 docker 벤치마크는 이 환경(torch/docker 부재)에서 못 돌리고, 돌려도 에피소드당 수 분이라 발표 전 측정이 비현실적이었다. → 임시로 고속 sim을 측정기로 쓰되, 신뢰할 만한 수치를 낼 수 있게 강화가 필요했다.

### 핵심 제약
- **발표 임박**: 폴더 물리 이동은 docker-compose·Dockerfile·grpc `sys.path`·`data/` 하드코딩 경로를 대량 파손시켜 발표 직전 리스크가 크다.
- **Q-테이블 보존**: `data/q_table.json`(416 상태)은 현 물리로 학습된 웜스타트. 상수 값이 바뀌면 무효화되어 재학습이 필요하다.

---

## 2. 결정 → 대안 비교 → 근거

| 결정 | 대안 | 근거 |
|---|---|---|
| **설정 통합 우선, 폴더 이동은 발표 후** | 지금 전체 기능별 재배치 | 이동은 import·경로 대량 파손 → 발표 직전 회귀 위험. 설정 통합은 저위험·고가치이며 측정 도구 강화의 전제조건. |
| **새 `sim_env.yaml` 신설(cost_model.yaml 유지)** | `environment.yaml` 하나로 전부 흡수 | 요금/GPU/회수확률은 이미 `cost_model.yaml`이 유일 진실. 흡수하면 마이그레이션 범위만 커지고 이득은 적음. 회수 기본확률 중복만 제거. |
| **값 무변경(비트 동일) 순수 이동** | 이동하며 값도 튜닝 | Q-테이블 재학습 회피 + 회귀 검증을 "값 동일" 단일 기준으로 단순화. 튜닝은 별도 단계로 분리. |
| **문맥상 다른 값은 강제 통합 안 함** | 모든 동명 상수를 하나로 병합 | `pretrain`의 태스크 유입확률(burst 0.067/normal 0.30)은 1틱=1초 축척 보정 리맵이라 실제(0.04/0.18)와 의도적으로 다름. scale-in 유예도 정책별 상이(dynamic 15s/sim 3s). 병합하면 동작이 바뀜 → `workload.pretrain`·`scale_in_sec_*`로 분리 보존. |

---

## 3. 변경 내역

### Phase 1 — 확률/환경 변수 단일 파일화 (값 무변경)
- **신규 `common/sim_env.yaml`**: failure(OOM/eviction/danger/out_of_capacity) · workload(생성확률·epochs·timeout·cap·pretrain 리맵) · budget · scaling · exec_time · reward · qlearning · state_buckets · scheduler_policy · scenarios. 모든 수치는 통합 시점 하드코딩과 비트 동일.
- **신규 `common/env_config.py`**: `cost_model.yaml`+`sim_env.yaml`을 1회 로드·캐싱하는 싱글턴. 내장 DEFAULTS(하드코딩 동일)로 딥머지 폴백 → 파일/키 부재나 yaml 미설치 환경에서도 동일 값 반환. 섹션별 접근자 제공.
- **소비 지점 치환(API·값 불변, 내부만 yaml 참조)**: `failure_simulator.py`(클래스 상수 + danger 사이클, 회수 기본확률은 `preemption_probs()`로 일원화해 폴백 중복 제거) · `state_features.py`(버킷 + 하드코딩 요금 제거) · `agent.py`(보상 가중치·하이퍼파라미터 기본값; 레거시 fallback 요금 0.710은 run_simulations 경제 보존 위해 미변경) · `state.py`(초기 예산) · `scheduler_daemon.py`(생성확률·cap·MAX_SPOT floor) · `dynamic.py`(선호/금지/스케일 임계) · `task_executor.py`(재시도 캡) · `pretrain.py`(전 상수·danger 함수 공유) · `run_simulations.py`(NODES_CONFIG←cost_model, 전 상수←sim_env).

### Phase 2 — `run_simulations.py` 발표용 강화
- **통계 신뢰성**: `FastSimulator(seed=…)` 시드 주입(하드코딩 `seed(42)` 제거). 모드별 N회(`--runs`) 반복 후 평균±표준편차·95% CI 집계. Q-Learning은 학습 1회 → 평가 N회 구조.
- **지표 확장**: 성공/SLA/처리량/건당비용, 연산 p50·p90, 지연 p90, **OOM/회수 분해**(exec_time==0→OOM), 모델별 성공률, 예산 생존율, 평균 노드 믹스.
- **시나리오 프리셋**: `sim_env.yaml scenarios`(normal/burst_heavy/low_budget/high_eviction)를 `--scenario a,b` 로 연달아 비교. 허용 오버라이드: 초기예산·burst/normal 확률·회수배율.
- **발표용 리포트**: 콘솔 비교표 + `data/benchmark_report_<scenario>.md`(핵심 결론 자동 문장 포함). 대표 1회분은 기존 12컬럼 CSV로 계속 저장해 `visualize_benchmarks.py`(`--chart`) 호환 유지.

### Phase 3 — 기능별 재배치 (설계만 확정, 미실행)
목표: `config/ scheduling/ learning/ simulation/ cluster/ workload/ observability/ transport/`. 이동 1순위는 의존이 깨끗한 `env_config`. 발표 후 별도 브랜치에서 import 일괄 수정 + docker 재빌드 검증.

---

## 4. 검증 (이 환경에서 수행)

- **값 무변경 회귀**: failure_simulator·state_features·state·agent·pretrain·run_simulations·dynamic의 상수가 통합 전 하드코딩과 비트 동일함을 assert로 확인.
- **동작 동일성**: `current_danger_phase`(5s→1/15s→0), `eviction_probability`(spot_a 위험 0.50/평시 0.125) 일치.
- **Q-테이블 정합**: `QLearningAgent()`가 기존 416-상태 테이블을 정상 로드(재학습 불필요).
- **오프라인 sim 스모크**: pretrain `SimulatedEnvironment` 3에피소드 정상.
- **측정 도구**: static/dynamic/q_learning 다회 실행 → 통계표·리포트·12컬럼 CSV·차트 생성 확인. q_learning 평가 성공률 ~68%로 기존 예산생존율과 정합.
- **미검증**: docker 실벤치는 이 환경 미지원(torch/docker 부재) → 사용자 환경에서 확인 필요.

---

## 5. 남은 일

- 시나리오 값 튜닝: 현 sim은 예산이 거의 안 마름(생존율 1.0) → `low_budget` 초기예산·`tasks_cap`을 발표 목적에 맞게 조정하면 예산 축 차이가 부각됨.
- Phase 3 폴더 이동(발표 후).
- 두 sim 엔진(`pretrain` vs `run_simulations`)은 상수는 공유하나 엔진 코드는 아직 분리 → `simulation/` 코어로 통합 여지.
