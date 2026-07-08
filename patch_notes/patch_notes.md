# 📝 Baby Ray 프로젝트 패치 노트 (Patch Notes & Error Logs)

본 폴더와 문서는 **Baby Ray** Docker 기반 분산 런타임 프로젝트의 구축 및 테스트 과정에서 발생한 핵심 시스템 오류들과 이를 해결하기 위한 패치 내역을 체계적으로 기록한 문서입니다.

---

## 📅 2026-07-08 패치 내역

### 1. [Q-Learning 스케줄러/물리 자원 고갈 대책] 호스트 리소스 고갈(`OutOfCapacity`) 방어 및 페널티 부여 구현
* **해결 및 패치 내용:**
  - Q-Learning 강화학습 에이전트가 실제 물리 자원 부족 상황(Docker `OutOfCapacity` 혹은 RAM 85% 상한선 초과)을 학습하지 못하고, 오직 증설(행동 4, 5)의 높은 Q-value에 의존해 무한히 스케일아웃을 트리거하여 대기열 태스크들이 기아(Starvation) 및 대규모 타임아웃 지연(150초 이상)에 빠지는 문제를 해결했습니다.
  - **이중 계층 자원 보호망:** 호스트 물리 메모리가 85.0%를 초과할 시, 증설 액션(행동 4, 5)을 선택할 수 없도록 스케줄러 의사결정 시점에 **하드웨어 인지형 액션 마스킹(Action Masking)**을 반영했습니다.
  - **동적 실패 페널티 보상 피드백:** 가상 공급 부족(simulated `OutOfCapacity`) 혹은 메모리 초과로 인해 스케일아웃이 거절되었을 때(`scale_out_worker()`가 `False`를 반환할 때), 에이전트에게 즉시 **`-10.0` 점의 강력한 음수 보상(페널티)**을 부여하여 Q-Table에 업데이트하도록 학습 로직을 고도화했습니다.

### 2. [벤치마크/데이터 정제] 노이즈 벤치마크 및 정적 백업본 정리
* **해결 및 패치 내용:**
  - 강화학습 에이전트가 탐험(Epsilon)을 활발히 하던 온라인 학습 시기에 쌓였던 노이즈 데이터들을 걸러내고 순수 추론(Inference) 단계의 신뢰도 높은 데이터를 얻기 위해 기존 `data/benchmark_results_q_learning.csv`를 삭제하고 초기화했습니다.
  - 이전 사전 학습 실행 시 호스트에 생성되어 가상 볼륨에 혼선을 주던 `head/q_learning/q_table.json` 정적 백업 파일을 최종 제거하여 단일 데이터 소스 정합성(`data/q_table.json`)을 확보했습니다.

### 3. [Q-Learning 스케줄러/오토스케일링] 도커 Cold Start 지연 보호 가드 구현 및 코드 리팩토링
* **해결 및 패치 내용:**
  - **도커 Cold Start 지연 보호 (Latency Masking):** 스팟 컨테이너 기동 지연(2~3초) 동안 에이전트가 중복 증설 명령을 내리지 못하도록, 생성되었으나 아직 GCS에 하트비트를 보내 등록하지 않은(LAUNCHING 상태의) 스팟 노드가 있을 경우 스케일아웃 행동(액션 4, 5) 선택을 일시적으로 차단(Masking)했습니다.
  - **Q-Table 저장 안정화 (Atomic Write):** Q-테이블 디스크 저장 시 발생할 수 있는 파일 손상을 원천 방어하기 위해, 임시 파일(`.tmp`)을 생성한 후 `os.replace`로 대치하는 원자적 쓰기(Atomic Write)를 구현했습니다.
  - **상태 직렬화 로직 단일화:** 사전 학습기, 시뮬레이터, 실시간 스케줄러 간의 상태 포맷팅 로직을 `agent.py`의 `QLearningAgent.state_to_str()` 정적 메소드로 일원화하여 코드 중복을 제거했습니다.
  - **학습/실험 데이터 초기화:** 개선 사항이 완전히 반영된 상태에서 인공지능이 무향(Clean) 상태로 사전 학습과 실시간 학습을 진행하고 대조군 실험을 신뢰도 있게 누적할 수 있도록 `data/` 내부의 모든 벤치마크 CSV 및 Q-테이블을 리셋했습니다.

### 4. [강화학습 보상 구조/신용 할당] 보상 즉시화(Immediate Reward) 및 상대 지연(Relative Delay) 구조 구현 완료
* **해결 및 패치 내용:**
  - **보상 즉시화 (Immediate Reward) 적용**: 완료 시까지 대기하던 기존의 비동기 보상 업데이트를 폐기하고, 배정 결정 시점에 예상 수행 시간 및 비용에 기반해 즉시 Q-value를 업데이트하는 온프레임워크 방식으로 전면 개편했습니다. 비동기 완료 콜백 스레드에서의 이중 갱신으로 인한 Q-Table 오염 문제를 해결했습니다.
  - **HOLD 행동 지연 페널티 부과**: 지연의 실질적 책임이 있는 HOLD(액션 3) 행동에 대해 대기열 지연 시간에 따른 벌점을 실시간 매 틱마다 부과하여 신속한 배정을 유도했습니다.
  - **증설(SCALE_OUT) 기회비용 페널티 부과**: 스팟 노드 증설(액션 4, 5) 시 각각 `-3.5`점 및 `-2.0`점의 비용 페널티를 부과하여 무분별한 증설 남발을 방지했습니다.
  - **상대 지연(Relative Delay) 기반 감점 도입**: 온디맨드 속도를 기준으로 추가되는 지연에 대해서만 속도 벌점을 부과함으로써 온디맨드 배정 시 항상 플러스 보상(`+3.9` ~ `+6.2`)이 보장되게 함으로써, ASSIGN이 항상 페널티만 받아 배정을 기피하던 버그를 해결했습니다.
  - **Q-Table 리셋 및 수렴**: 기존 벌점 오염을 제거하기 위해 `data/q_table.json`을 `{}`로 초기화하고 실시간 학습을 진행하여, Q-value가 실시간으로 양수값으로 정상 수렴하는 것을 확인했습니다.
  
### 5. [강화학습 알고리즘/탐색 제어] Epsilon 탐험률 자연 감쇄(Decay) 및 로드 로직 복원
* **해결 및 패치 내용:**
  - `q_learning.py`에서 온라인 훈련 모드일 때 탐험률(Epsilon)이 0.1 미만으로 내려가면 강제로 0.1로 올리던 바닥선(floor) 가드를 제거하여, `decay_rate=0.995`에 의해 `epsilon_min`인 0.05까지 자연스럽게 감쇄하도록 복원했습니다.
  - `agent.py`의 `load_q_table()` 메서드 내에 분기를 적용하여, `Q_LEARNING_TRAINING_MODE = True`(온라인 추가 학습 진행)인 상황에서는 Epsilon 강제 최소화 설정을 우회하고 점진적 감쇄가 이어지도록 정합화했습니다.

### 6. [Q-Learning 벤치마크/실험 로깅] 학습 모드 시 성능 누적 로깅 복원 (롤백)
* **해결 및 패치 내용:**
  - `task_executor.py`에서 `log_benchmark_metric` 호출부에 걸려있던 `if not (q_learning and training)` 차단 조건문을 완전히 제거(롤백)하여, `training=True` 상태에서도 `data/benchmark_results_q_learning.csv`에 매 태스크 완료 성능 지표가 무관하게 계속 누적 적재되도록 수정했습니다.

### 7. [Q-Learning 스케줄러/가용 행동 필터] 실시간 메모리 기반 배정 차단(mem < 90.0) 가드 해제
* **해결 및 패치 내용:**
  - `q_learning.py` 내의 가용 액션 탐색부와 워커 바인딩 루프에서 `info.get("mem", 0.0) < 90.0` 제약을 제거하여, 워커의 메모리가 90% 이상 차 있더라도 `IDLE` 상태인 워커라면 정상적으로 작업을 배정받아 실행할 수 있도록 버그를 수정했습니다.
  - 이를 통해 WSL2 및 부하 시뮬레이션 환경의 메모리 고점 보고로 인해 스케줄러가 배정 액션을 영구 포기하고 HOLD와 SCALE_OUT만 무한 반복하던 Starvation 현상을 최종 해결했습니다.

### 8. [Q-Learning 상태 공간/비용 제어] `b_avail` (가상 예산 잔여량)에서 `c_level` (시간당 비용 소모 수준)로의 상태 대체
* **해결 및 패치 내용:**
  - 기존의 `b_avail`은 가상 예산 잔여량이 $0.7 이하로 떨어질 때 `0`이 되는 지표였으나, 예산 소모가 너무 느려 단기 실행 시 항상 `1`에 고착되어 상태 공간을 정적화하고 학습을 방해했습니다.
  - 이에 따라 `common/cost_model.yaml`의 실제 10배 스케일링된 단가(On-Demand: $7.10/hr, Spot-A: $2.20/hr, Spot-B: $0.90/hr)를 반영하여, 기동 중인 워커 노드들의 시간당 비용 소모율(Burn Rate) 임계치를 **`$9.00/hour`**로 설정하고 이를 이진화한 **`c_level` (총비용 수준)**로 대체했습니다.
  - 고비용 상태(`c_level = 1`, 예: 1 OD + 1 Spot-A 또는 1 OD + 3 Spot-B 가동) 시 추가 Spot-A/B 노드 증설(SCALE_OUT)에 벌점을 매겨, 불필요한 예산 낭비를 실시간으로 자제하도록 학습 효율을 고도화했습니다.

### 9. [Q-Learning 상태 공간/SLA 긴박도] `u_sla` (SLA 마감 임박) 임계 지표 단축 (30.0초 -> 5.0초)
* **해결 및 패치 내용:**
  - 시뮬레이션 환경에서 태스크가 큐에 삽입될 때 부여되는 마감 기한(Deadline)이 보통 생성 시간 기준 5초~12초 후로 설정되어, 기존 임계치(30.0초 이하) 하에서는 모든 태스크가 유입 즉시 `u_sla = 1`로 고정되는 학습 왜곡 문제가 있었습니다.
  - 이에 따라 `u_sla` 판정 임계치를 **`5.0초 이하`**로 단축 조정하여, 대기열에서 실제로 5초 이하로 지연된 태스크만 동적으로 긴박 상태(`1`)로 전이되어 에이전트가 최적의 스케줄링 가중치를 학습할 수 있도록 수정했습니다.

---

## 📅 2026-07-07 패치 내역

### 1. [Q-Learning 스케줄러/Action Masking] SLA 마감 임박/초과 시 HOLD 방지 룰 반영
* **해결 및 패치 내용:**
  - 마감 기한이 임박하거나 이미 초과한 태스크가 대기열 앞단에 존재하고 가용 온디맨드 노드가 있음에도 배정을 피하고 무한 HOLD 루프 및 불필요한 스팟 증설/회수 루프에 빠지던 문제를 해결하기 위해 `head/scheduler/q_learning.py`에 Action Masking 안전 장치를 도입했습니다.
  - 마감 임박/초과 상태(`u_sla == 1`)이고 가용 노드가 1대 이상 존재할 경우, 에이전트 행동 선택지에서 **HOLD(Action 3)** 행동을 강제 필터링하여 제외시켰습니다.
  - 패치 이후 큐의 태스크들이 가용 노드로 즉각 배정되어 연산이 진행되었고, `data/benchmark_results_q_learning.csv`에 성공/실패 실험 지표가 정상 적재됨을 확인했습니다.

### 2. [강화학습 보상 구조/알고리즘 고찰] 비동기 보상 타이밍의 한계 분석 및 개선 방향 수립
* **해결 및 패치 내용:**
  - 지연 벌점 폭탄이 최종 배정(ASSIGN) 행동에 한꺼번에 덤터기 씌워져 Q-value가 비정상적으로 추락하는 신용 할당 오류를 방어하고, 스팟 증설(SCALE_OUT)의 제자리 루프 양수 편향 학습 문제를 완화하기 위해 개선 설계 분석을 진행했습니다.
  - **보상 즉시화**: 태스크 완료 콜백까지 대기하는 비동기 갱신을 폐기하고 의사결정 시점에 즉각 Q-value를 업데이트하여 MDP 정합성을 보장하도록 개선 방향을 잡았습니다.
  - **HOLD 행동에 점진적 지연 벌점 부과**: 지연 페널티를 배정이 아닌 HOLD 시점에 매 step마다 누적 감점하도록 설계하여 에이전트의 대기 회피 학습을 정상화하기로 했습니다.
  - **사전 학습 시뮬레이터 개선**: `pretrain.py`가 상태 전이를 완전히 랜덤으로 처리하던 방식을 고쳐, 배정 시 노드 상태 점유 및 큐 감소 등의 물리적 전이 모델을 이식하기로 설계했습니다.

---

## 📅 2026-07-05 패치 내역

### 1. [실험 데이터 수집/벤치마크] 자동 실험 데이터 누적 기록 (Benchmark Metric Logger) 도입
* **해결 및 패치 내용:**
  - 3대 스케줄러(Static, Dynamic, Q-Learning)의 성능, 비용, SLA 지연시간 변동을 일관되게 비교 검증할 수 있도록 `head/scheduler/utils.py` 내부에 `log_benchmark_metric()` 함수를 신설했습니다.
  - 태스크가 완료(SUCCESS)되거나 실패(FAILED/Eviction)하는 매 시점마다 **Timestamp, 모드, 태스크 ID, 모델 종류, 상태, 수행시간, 가상 비용, SLA 초과 지연 초, 활성 온디맨드/스팟A/스팟B 대수, 남은 예산**을 `data/benchmark_results.csv` 파일 끝에 실시간 누적 적재합니다.
  - 마운트된 볼륨 내에 저장된 데이터를 호스트 파일시스템 영역으로 즉시 공유 연동하여, 사용자가 외부 분석기 없이도 판다스(Pandas)나 엑셀로 원천 데이터를 분석할 수 있는 환경을 제공합니다.

### 2. [Dynamic 스케줄러/오토스케일링] 부하 및 모형 적응형 이종 스팟 인스턴스 스케일 제어 구현
* **해결 및 패치 내용:**
  - 기존 Dynamic 스케줄러가 비용 효율이 떨어지는 Spot-A만 고정 증설/회수하던 방식을 전면 리팩토링했습니다.
  - **이종 적응형 스케일아웃**: 평균 리소스 부하가 매우 높거나(CPU > 75% 또는 Memory > 70%), 대기 큐 내에 메모리 오버헤드가 막중한 `LSTM` 태스크가 포함된 경우에만 고성능 **Spot-A**를 증설하고, 그 외의 일반 적체 상황에서는 비용 효율적인 **Spot-B**를 증설하도록 개편했습니다.
  - **비용 최적화 스케일인**: 유휴 상태로 진입 시 단가가 비싼 **Spot-A를 최우선으로 회수**하고, Spot-A가 전량 감축된 이후 Spot-B를 회수하여 시간당 요금 소모 속도(Burn Rate) 방어력을 향상시켰습니다.
  - GCS 스팟 대수 합산 함수(`get_current_spot_scale` in `core.py`)가 Spot-A와 Spot-B 모두의 누적 기동 대수를 집계하도록 수정하여 전체 스팟 인프라 상한선(`MAX_SPOT_SCALE`) 제한의 안정성을 보장했습니다.

### 3. [스케줄러/HOL Blocking 해소] 비순차 백필링 (Backfilling) 알고리즘 탑재
* **해결 및 패치 내용:**
  - 가용 자원이나 특정 노드 타입의 부족으로 대기열 맨 앞의 태스크가 배정되지 못해 락에 빠지는 **HOL(Head-of-Line) Blocking** 현상을 해결하기 위해 백필링 정책을 수립했습니다.
  - Dynamic 스케줄러(`head/scheduler/dynamic.py`) 및 Q-Learning 스케줄러(`head/q_learning/scheduler.py`) 의사결정 루프를 수정하여, 배정 실패한 태스크를 보류열(`deferred_tasks`)로 우회 적재한 뒤 후순위 대기 태스크들의 가용 노드 매핑 및 선제 할당 작업을 지속합니다.
  - 큐 탐색이 완료되면 보류되었던 태스크들을 원래의 선입선출(FIFO) 우선순위 순서를 보존하여 큐 선두로 다시 적재(`insert(0, task)`)함으로써 기아 현상(Starvation)을 방지합니다.

### 4. [리소스 격리/cGroup 리사이징] CFS 주기 기반 Docker CPU 동적 크기 조절 구현 및 409 충돌 해결
* **해결 및 패치 내용:**
  - 메모리 집약형 `LSTM` 태스크 기동 시 해당 워커의 cGroup 메모리 한도를 `1.5배` 임시 상향(Spot-B 기준 768MB -> 1152MB) 조정하고, 태스크 완료 시 원본 규격으로 복원하도록 `head/scheduler/utils.py` 내부에 `adjust_worker_resources()` 리사이징 프로토콜을 구현했습니다.
  - **409 Conflict 오류 완벽 해결**: 최초 기동 시 `--cpus`(NanoCPUs) 제한 인자가 주입된 컨테이너는 Docker Engine 명세상 실행 중에 `cpu_quota` / `cpu_period` 업데이트가 원천 금지되는 제약이 있었습니다.
  - 이를 위해 `docker-compose.yml` 및 `cluster_manager.py` 내의 컨테이너 초기 기동 시점의 NanoCPUs 설정을 전면 걷어내고, CFS 스케줄러 주기(`cpu_period=100000`, `cpu_quota=int(cpu_limit * 100000)`) 단위로 제어 아키텍처를 일원화하여 실시간 리사이징이 충돌 없이 온전하게 수행되도록 최종 해결했습니다.

### 5. [사전 학습/요율 동기화] cost_model.yaml 기반 시뮬레이터 요율 정합화 및 Q-Table 재학습
* **해결 및 패치 내용:**
  - 오프라인 사전 학습기(`head/q_learning/pretrain.py`) 내부에 하드코딩되어 불일치했던 가상 비용 변수들을 실제 `cost_model.yaml` 요율인 On-Demand $0.710, Spot-A $0.220, Spot-B $0.120 수준과 정확히 1:1 일치시켰습니다.
  - 동기화된 요율 구조 하에서 25,000 에피소드 학습 시뮬레이터를 실행하여 가상 예산 고갈 조건 및 벌점 보상이 수렴된 신규 `q_table.json` 모델 파일을 생성 완료했습니다.

### 6. [헬스체크/안정성] DEAD 워커 감시 하트비트 오차 임계치 상향 (3.0초 -> 5.0초)
* **해결 및 패치 내용:**
  - Windows 호스트 환경(WSL2 기반 Docker Desktop) 하에서 다중 PyTorch 컨테이너 스케일아웃 가동 시, 컨테이너 부팅 과정의 순간적인 CPU 스파이크 및 네트워크 병목으로 인해 실제 생존 중인 워커의 하트비트 응답이 3초 이상 transient 지연되는 현상을 확인했습니다.
  - 이로 인해 워커 노드가 거짓-오프라인(False-Dead) 처리되어 탈퇴와 스케일아웃 재기동이 무한 반복(Flapping)되는 현상을 방지하기 위해, `core.py` 내의 DEAD 노드 판정 오프라인 타임아웃 임계치를 기존 3.0초에서 **5.0초**로 소폭 확장하여 클러스터 안정성을 크게 확보했습니다.

---

## 📅 2026-07-04 패치 내역

### 1. [GCS 영속화/장애복구] GCS 상태 영속 체크포인팅 도입
* **해결 및 패치 내용:**
  - Head 마스터 노드 크래시나 컨테이너 재시작 시 메모리에만 존재하던 GCS 메타데이터(대기열, 태스크 상태, Lineage 관계, 가상 예산, 태스크 카운터 등)가 전량 증발하는 문제를 해결하기 위해 `/app/data/gcs_state.json` 영속 JSON 상태 백업 본을 도입했습니다.
  - `state.py` 내에 `save_gcs_state()` 및 `load_gcs_state()` 스레드 안전 함수를 신설하고, `head.serve()` 최초 진입 시 로드하여 중단 지점부터 투명하게 리플레이(Replay) 하도록 연동했습니다.
  - 태스크 기입, 서브태스크 완료, 라인업 소거, 장애 복구 등 GCS 상태 전이 지점마다 실시간 상태 영속화가 자동 유발되도록 패치했습니다.

### 2. [헬스체크/동기화] DEAD 워커 감지 하트비트 임계치 3초 단축
* **해결 및 패치 내용:**
  - 기술제안서(Technical Proposal) 명세인 "생존 유실 판정 임계치 3.0초" 규격에 맞추어, 기존 15.0초로 설정되어 있던 DEAD 노드 판정 임계치를 **3.0초**로 일괄 단축하고 헬스체크 정밀도를 극대화했습니다.
  - 이로써 스팟 노드가 갑작스럽게 Eviction/Preemption 되더라도 최대 3초 이내에 유실을 파악하고 즉각 자가 복구 조치에 착수할 수 있게 되었습니다.

### 3. [태스크 복구/최적화] Task Lineage 기반 Skip-Execution & 최신 체크포인트 이어서 재개 (Re-execution) 구현
* **해결 및 패치 내용:**
  - **Skip-Execution 구현:** 맵-머지(Map-Merge) 서브태스크 복구 시, 이미 최종 완료된 서브태스크 결과 파일(`data/final_{sub_task_id}.pt`)이 공유 볼륨에 존재하면 이를 건너뛰고 스킵 처리하여 가용 비용을 절약합니다.
  - **이어서 재개(Re-execution) 구현:** 최종 파일은 없지만 중간 학습 성과 체크포인트 파일(`data/checkpoint_{sub_task_id}_epoch_{ep}.pt`)이 검출될 경우, **최신 에포크의 체크포인트 가중치를 로드하여 남은 에포크만큼만 이어서 학습을 재개**하도록 맵 서브태스크 기동 및 DEAD 노드 복구 모듈에 전격 반영했습니다. (처음부터 다시 학습하는 오버헤드를 완벽 차단)

### 4. [오토스케일링/연동] 장애 복구 시 대체 자원 자동 증설 (Auto Scale-Out) 연동
* **해결 및 패치 내용:**
  - 스팟 워커 탈퇴로 인해 장애 복구(Cascaded Recovery)가 발동하여 유실된 서브태스크가 대기열로 복구되는 시점에, 즉시 대체 노드를 공급하여 클러스터 처리량을 보전하기 위한 자동 스케일아웃(`scale_out_worker("spot_a")`)을 자동 연동했습니다.

### 5. [긴급 버그 패치/오토스케일링] docker-compose 내 Spot-B (worker-3) 정적 기동 복구 및 MPS 연동
* **해결 및 패치 내용:**
  - 사용자 환경의 기본 번들 테스트 시나리오 실증을 위해 `docker-compose.yml` 내의 Spot-B (`worker-3`) 정적 컨테이너 구동 블록을 주석 해제하여 복구했습니다.
  - 동시에 실전형 물리 GPU 자원 격리 환경 구성을 위해 `worker-3` 및 `worker-1`에 `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` 환경 변수를 주입했습니다.

### 6. [Host RAM 최적화] 최대 동적 워커 스케일 상한선 축소 (`MAX_SPOT_SCALE = 3`)
* **해결 및 패치 내용:**
  - 16GB RAM 로컬 PC 개발 환경에서 다중 PyTorch 컨테이너(최대 9대)가 동시 실행될 시 발생하는 극심한 Host RAM 부족 및 프리징 문제를 해결하기 위해, 동적 스팟 워커 상한선(`MAX_SPOT_SCALE`)을 기존 7대에서 **3대**로 조절했습니다.
  - 이로써 PyTorch 임포트 자체로 인한 Host RAM 기본 소모량을 기존 약 6.3GB대에서 **2.1GB대**로 대폭 경감하여 PC의 안정성을 확보했습니다.

### 7. [물리 자원 격리/MPS] NVIDIA MPS 기반 실전형 GPU 하드웨어 격리 도입 및 지연 모사 제거
* **해결 및 패치 내용:**
  - 소프트웨어 단위로 `time.sleep`을 주입하여 가상 속도를 모사하던 인위적인 딜레이 코드를 완전히 걷어냈습니다.
  - 대신 호스트에서 구동 중인 **NVIDIA MPS(Multi-Process Service)**의 물리적인 CUDA 스레드 제한 기능(`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE`)을 활용하도록 컨테이너 및 스케일아웃 기동 로직에 환경 변수(`spot_a`: 60, `spot_b`: 30)를 연동했습니다.
  - 이제 가상의 대기 시간이 아니라 물리적으로 할당된 CUDA 코어 스레드 수에 비례하여 에포크 연산 시간이 자연스럽게 증감하게 됩니다.

### 8. [Host RAM 최적화] GPU Direct 합성 데이터셋 생성 및 캐싱 구현
* **해결 및 패치 내용:**
  - 매 에포크마다 CPU 상에서 가상 MNIST 및 시계열 데이터셋 텐서를 반복 생성하여 GPU로 전송(`images.to(device)`)하던 로직이 Host RAM에 무거운 가비지를 적체시키는 원인을 분석했습니다.
  - `get_inline_mnist_dataset(device)` 및 `RNNTask.train_epoch()`에서 CPU를 거치지 않고 **GPU(CUDA) 상에서 합성 텐서를 직접 생성**하도록 리팩토링하고, `CNNTask` 내부에 한 번 로드된 데이터셋을 **캐싱**하도록 개선하여 Host RAM 소모량을 거의 0에 가깝게 최적화했습니다.

### 9. [대시보드/시각화] 실시간 과금 분석 및 스케일 변동 타임라인과 장애/FedAvg 펄스 애니메이션 도입
* **해결 및 패치 내용:**
  - **가상 자산 & 과금 분석 카드**: 잔여 예산과 함께 실시간 누적 소비 비용, 예산 소모율 진행 바(Progress Bar), 현재 기동 중인 워커 가격표를 반영한 실시간 시간당 소모 비용율(`$/hr` Burn Rate)을 도입했습니다.
  - **실시간 자원 변동 및 회수 이력 카드**: 텍스트 로그를 실시간 스캔하여 스케일아웃, 스케일인, 강제 preemption(Eviction) 이벤트를 감지 및 타임라인 배지로 렌더링하도록 구현했습니다.
  - **장애/선점 회수 노드 적색 퇴출 애니메이션 (`exit-failed`)**: 워커가 사라지는 시점에 마지막 하트비트가 지연되었거나 유휴가 아닌 연산 중 갑자기 사라졌다면 장애/선점 회수로 판정하여 카드가 붉은 경고광을 내며 퇴출되는 효과를 추가했습니다.
  - **FedAvg 동작 펄스 애니메이션**: 분할 학습 단계 시 보라색 펄스(`MAP`), 병합 단계 시 청록색 박동 펄스(`MERGE`)를 노드에 입혀 분산 알고리즘 제어 동작의 흐름을 한눈에 식별할 수 있도록 개선했습니다.

### 10. [자가데드락/안정화] 글로벌 GCS 락 재진입 가능 RLock 도입 및 타이밍 불일치 수정
* **해결 및 패치 내용:**
  - **자가 교착상태(Self-Deadlock) 예방:** 모의 태스크 생성 또는 DEAD 노드 회수 시 `queue_lock`이나 `registry_lock`을 획득한 상태에서 내포된 `save_gcs_state()`가 락을 재요청하며 발생하던 자가 데드락 문제를 전역 락의 재진입 가능 뮤텍스(`threading.RLock`) 교체 및 호출부 격리(with 블록 분리)를 통해 해결했습니다.
  - **상시 비용 차감 루프 활성화:** 핵심 스케줄러 루프(`head/scheduler/core.py`) 내에 실시간 비용 차감 루틴을 정상 이식하여 워커 노드 구동 시 대시보드 예산 소비가 실시간으로 반영되도록 개선했습니다.
  - **하트비트 타이밍 매칭 최적화:** DEAD 워커 판정 임계치가 3.0초인 상황에서 하트비트 전송 주기(`DEFAULT_HEARTBEAT_INTERVAL`)가 5.0초로 설정되어 노드가 항상 거짓-오프라인(False-Dead) 판정을 받던 주기를 **1.0초**로 단축 및 동기화했습니다.
  - **대시보드 렌더러 예외 처리 강화:** 브라우저 캐싱으로 인한 프론트엔드 HTML/CSS/JS 버전 불일치 TypeError 크래시를 전면 방지하기 위해 `app.js` 내에 완전한 DOM Null-Safety 방어 코드를 구현하고 HTTP 서버 단에 `Cache-Control` 캐시 무효화 헤더를 삽입했습니다.

### 11. [스펙 차별화/연동] Spot-A와 Spot-B 스펙의 이종 트레이드오프화 및 Docker/MPS 동적 연동
* **해결 및 패치 내용:**
  - **이종 트레이드오프 설계:** Spot-B가 Spot-A의 단순 하위 호환이던 문제를 해결하기 위해, **Spot-A (성능 지향 / 고휘발성)**와 **Spot-B (안정/비용 지향 / 저휘발성)**의 뚜렷한 아키텍처 특성 차이를 정의했습니다:
    * **Spot-A**: CPU 1.5 Cores, Memory 1.5GB, GPU MPS 85%, Preemption 확률 50% ($0.220/Hour)
    * **Spot-B**: CPU 0.8 Cores, Memory 768MB, GPU MPS 35%, Preemption 확률 10% ($0.120/Hour)
  - **Docker SDK cGroup & MPS 동적 로드:** `cluster_manager.py` 내의 하드코딩된 동적 워커 생성 제한 설정을 걷어내고, `cost_model.yaml` 명세에 기반하여 `nano_cpus` 및 `mem_limit`, 그리고 NVIDIA MPS 스레드 제한(`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` = `gpu_scale_factor * 100`)을 컨테이너 생성 시 동적 주입하도록 패치했습니다.
  - **스팟 회수율(Preemption) 연동:** 백그라운드 `eviction_loop`에서 각 스팟 워커 타입의 고유 `preemption_probability`를 `cost_model.yaml`로부터 로드하여 난수 평가에 동적 매핑하도록 구현하여, Spot A는 요금제 변동기(P_spot=1)에 매우 자주 회수되는 반면 Spot B는 안정적으로 생존하는 아키텍처 차이를 완성했습니다.
  - **정적 워커 및 대시보드 요율 동적화:** `docker-compose.yml` 내의 정적 Spot B 워커(`worker-3`) 명세를 CPU 0.8 / RAM 768M / MPS 35%로 일원화하고, 대시보드 프론트엔드(`app.js`)에서도 하드코딩된 시간당 비용 테이블을 제거해 GCS의 `nodes_config` 데이터에 의거한 동적 Burn Rate 계산을 구현했습니다.

---

## 📅 2026-07-03 패치 내역

### 1. [체크포인트/종료] 중간 가중치 체크포인트(.pt) 자동 소거 및 Graceful Shutdown 고도화
* **해결 및 패치 내용:**
  - 학습 성공 완료 시점에 누적 쌓여서 디스크 공간을 낭비하던 `data/checkpoint_*.pt` 임시 파일들을 `glob` 모듈을 도입하여 일괄 자동 소거했습니다. (Map-Merge 서브태스크 및 단일 일반 학습 성공 시 즉시 삭제, 실패 시 복구용 보존 유지)
  - **컨테이너 즉각 강제 삭제 최적화:** 좀비 컨테이너 청소 시 개별 `container.stop(timeout=2)` 호출 단계를 제거하고, `container.remove(force=True)`만 단독 호출하여 수 밀리초 내에 즉시 동적 워커 노드를 강제 회수하도록 개선하여 종료 시 Docker 데몬의 강제 SIGKILL(10초 제한) 타임아웃 문제를 해결했습니다.
  - **볼륨 파일 강제 정화:** `handle_shutdown()` 종료 핸들러 내에 공유 볼륨 내부의 모든 잔존 가중치 파일(`data/checkpoint_*.pt` 및 `data/final_*.pt`)을 즉시 소거하는 정화 루틴을 구축하여 볼륨 누수를 완벽 차단했습니다.

### 2. [추론검증/대시보드] FedAvg 수학적 가중치 결합 모델의 실제 추론 결과 연동
* **해결 및 패치 내용:**
  - 기존의 분산 병합 최종 결론이 3개 분할 맵 노드의 임시 텍스트 결과를 단순 파이프(`|`)로 이어 붙이거나 다수결로 산출되어 결과 전달이 불투명했던 문제를 개선했습니다.
  - 병합 작업을 전담한 워커의 연산 로그(`r_stat.logs`)로부터 수학적으로 평균 병합 완료된 모델의 직접 추론 검증 로그(`[FedAvg Verification]`) 라인을 직접 파싱/추출하여 이를 최종 분석 결론으로 설정했습니다. 이로써 3개 워커의 학습 성과가 융합된 고품질의 완성형 추론 문장 하나가 대시보드 화면에 명확하게 노출됩니다.

### 3. [비용모델/인프라] 실시간 노드 상시 구동 비용 차감 (Continuous VM Billing) 도입
* **해결 및 패치 내용:**
  - 기존 완료 시점의 일괄 누적 차감 방식(`task_cost` 차감)을 비활성화하고, 가동 중인 모든 워커 노드의 시간당 가동 비용을 초 단위로 환산하여 매 초마다 가상 예산에서 실시간 차감하는 상시 구동 비용 모델로 개편했습니다.
  - 동적 스케일아웃으로 Spot 노드를 켜놓고 방치 시 가상 예산이 빠르게 드레인(Drain)되도록 시뮬레이션함으로써, 에이전트가 보다 자율적인 스케일인(Scale-in)의 필요성과 저비용 Spot-B 노드를 적극 활용하도록 비용-성능 트레이드오프 관계를 강력하게 수립했습니다.

### 4. [자원회수/스케일링] 노드 유휴 시간(Idle Duration) 감지 기반 탄력적 스케일인(Scale-In) 개편
* **해결 및 패치 내용:**
  - 기존 대기 큐 크기 0일 때만 작동하던 제약 조건을 개편하여, 대기열 적체 여부와 상관없이 클러스터 내에 유휴(IDLE) 상태인 스팟 노드가 감지되는 즉시 타이머를 측정합니다.
  - 유휴 상태가 누적 10초 이상 유지되면 초과 증설된 자원으로 감지하여 즉시 자율 순차 회수(Scale-in)를 단행하여 예산 낭비를 자율 방어하도록 보완했습니다.

### 5. [명칭교체/대형화] 'REDUCE' 단계 명칭의 'MERGE' 단계 전환 (Head 스케줄러 & 대시보드)
* **해결 및 패치 내용:**
  - 분산 연산 병합 단계의 정체성을 강화하고자 Head 스케줄러 및 대시보드 프론트엔드의 `REDUCE` 태스크/모델명 및 접미사를 `MERGE` 및 `-merge`로 일괄 교체했습니다.
  - 벤치마크 테스트 스크립트(`run_benchmark.py`) 내부의 4차원 상태 공간 리팩토링 정합성을 복구하고 6대 액션 공간 매핑을 맞춰 벤치마크 테스트의 신뢰성을 복원했습니다.

---

## 📅 2026-07-01 패치 내역

### 1. [GCS/안정화] `RegisterWorker` 중복 ID 등록 세션 충돌 방지 로직 신설
* **해결 및 패치 내용:**
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)의 등록 API 내부에 중복 검증 로직을 추가했습니다. 등록 시점에 `worker_id`가 이미 등록되어 있다면 경고 로그를 남기고 실패 응답(`success=False`)을 보내어 세션 침범을 완벽히 방어하였습니다.

### 2. [인프라/스케일링] 이기종 Spot-B 워커 노드 동적 스케일링 완전 연동
* **해결 및 패치 내용:**
  - [head/cluster_manager.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/cluster_manager.py)의 `scale_out_worker()` 함수 내에 **Spot-B (0.5 Core, 512MB RAM, 시작 포트 50070)** 사양을 추가하였습니다.
  - 스케일아웃 시 `spot_a`는 `worker-2` 계열, `spot_b`는 `worker-3` 계열로 컨테이너 접두어를 분기하여 부팅 시 좀비 클린업 로직과 정합성을 맞추었습니다.
  - Spot-B 워커를 백그라운드 Eviction Daemon(강제 스팟 회수) 및 OutOfCapacity 모사 로직에 완벽히 연동하였습니다.

### 3. [자원가드/확장성] 호스트 물리 메모리 가드(Safety Guard) 상향 및 우회 변수 제공
* **해결 및 패치 내용:**
  - 스케일아웃 전 시스템 자원 상태를 검증하는 `is_host_resource_sufficient()`의 가용 메모리 임계 안전선을 기존 `2.0 GB`에서 **`4.0 GB`**로 상향하고, PC의 급격한 과부하(Swap 지연 및 VM 다운)를 사전에 차단하기 위한 해결 방법 로그를 출력하도록 개선했습니다.
  - `$env:BYPASS_RESOURCE_GUARD="1"` 환경 변수 입력 시 가용 램 점검 가드를 즉시 우회(Bypass)할 수 있는 가드 우회 옵션을 신설하여 로컬 개발 테스트 편의성을 대폭 보강했습니다.
  - [head/scheduler/core.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트\WE-MEET/head/scheduler/core.py)에 정의된 동적 스팟 워커의 최대 가동 대수 제한(`MAX_SPOT_SCALE`)을 기존 3대에서 **7대**로 확장하여 더 다이내믹한 이기종 스케일링이 가능하도록 개선했습니다.

### 4. [인프라 제어/격리] PyTorch 물리 GPU VRAM 할당 격리 제한 (VRAM Guard) 구현
* **해결 및 패치 내용:**
  - [worker/gpu_simulator.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/gpu_simulator.py) 내부 `PyTorchTaskRunner.run()` 연산 개시부 직후에 `torch.cuda.set_per_process_memory_fraction` API를 사용해 각 워커가 물리적으로 접근할 수 있는 CUDA 메모리 한도를 강제 지정하였습니다.
  - **노드 등급별 VRAM 격리 제한:** On-Demand(4.0 GB), Spot-A(2.0 GB), Spot-B(1.0 GB). 지정량 초과 점유 시 CUDA OOM 예외 발생을 유도하여 하드웨어 자원 격리 가드를 완성했습니다.

### 5. [긴급 버그 패치] 빌드 오류, 데이터셋 404 및 맵-리듀스 데드락 해결
* **NameError & ImportError 해결:** `cluster_manager.py` 내의 누락되었던 필수 표준 라이브러리(`time`, `random`, `threading`)를 바인딩하고, `gpu_simulator.py` 내 지워진 레거시 모듈에 대한 임포트 참조를 제거하여 워커/헤드 크래시를 완벽 해결했습니다.
* **MNIST 외부 404 차단 바이패스:** 외부 AWS S3 만료로 인한 torchvision 다운로드 404 실패 이슈를 해결하기 위해, 코드 내부에서 0~9 손글씨 이미지를 직접 다차원 텐서로 주입하는 **`get_inline_mnist_dataset()` 인라인 가상 데이터셋**을 구축했습니다.
* **Map-Reduce 데이터 볼륨 마운트 및 핀닝:** 격리된 컨테이너 간 가중치 전송용 도커 볼륨 마운트를 구성하고, 스팟 탈퇴로 인한 회수 방지를 위해 병합(Reduce) 연산을 `on_demand` 노드로 고정(Pinning)했습니다.
* **스케줄러 데드락 해결:** 맵 작업 분산 시 온디맨드 노드가 메인 스레드 락에 걸려 `IDLE`로 풀리지 못하던 순환 대기 데드락을 맵-리듀스 진입 시 락을 해제하도록 보강하여 완치했습니다.

### 6. [네트워크/셧다운] Graceful Shutdown 및 좀비 스팟 컨테이너 완전 소거
* **시그널 리스너 기반 자동 제거:** `Ctrl+C` 또는 `docker-compose down` 시 헤드 프로세스가 `SIGINT/SIGTERM` 시그널을 감지하여 구동 중이던 모든 동적 스팟 워커들을 도커 SDK로 즉시 동기 정리 소거하는 셧다운 핸들러를 장착했습니다.
* **외부 브릿지 네트워크 전환:** 컨테이너 정리 지연으로 네트워크 해제가 실패하던 문제를 `babyray-net` 을 external 네트워크로 매핑하여 영구 해결했습니다.

### 7. [GUI/대시보드] 파일 독립화 및 프리미엄 라이브 스케일 애니메이션 구축
* **프론트엔드 Decoupling:** `server.py` 내 하드코딩된 HTML/CSS/JS 코드를 독립 정적 웹 리소스로 완전히 물리 분리했습니다.
* **학습/SLA 지표 배너 추가:** 대시보드 상단에 스케줄러 모드, 완료/실패 수, 강화학습 탐험율(Epsilon) 실시간 현황판을 추가했습니다.
* **이원화 풀 및 스케일 애니메이션:** 온디맨드와 스팟 노드 풀을 레이아웃으로 분리하고, 노드 가입/탈퇴 시 부드러운 스케일인/아웃 페이드 트랜지션 애니메이션 효과를 구현했습니다.

### 8. [고가용성] Task Lineage DAG 자가 복구 (Cascaded Recovery) 구현
* **의존성 계보 스냅샷:** `state.py` 에 `task_lineage = {}` 공유 딕셔너리를 신설하여 서브맵 태스크들의 부모 관계, 실행 담당 워커 ID, 훈련 파라미터 및 런타임 상태를 실시간 관리합니다.
* **장애 자동 구조:** `core.py` 가 DEAD 워커 감출 즉시 유실된 작업을 큐 맨 앞으로 긴급 복구(`task_queue.insert(0, task)`)하여 대체 워커 노드에서 즉시 재훈련이 이루어지도록 고가용성을 패치했습니다.

### 9. [강화학습] 보상 함수 리셰이핑을 통한 Co-scheduling 자율 학습 구현
* **자율 융합 배치:** `agent.py` 보상 함수에 `co_scheduled_models` 와 `current_model` 분석을 연동하여, 동일 노드 내에 이종 모형(CNN vs RNN/LSTM)이 융합 배치되면 보상을 가산(+0.15)하고, 자원 경합 유발 시 감산(-0.20)하여 강화학습으로 자율적인 Co-scheduling이 수렴되도록 보완했습니다.

### 10. [클린업/평가] 중간 맵 파일 소거 및 최종 연합 학습 추론/결과 평가 출력 연동
* **공간 누수 소거:** `utils.py`에서 Reduce 병합 완료 시점에 결합 재료였던 임시 맵 가중치 파일들(`.pt`)을 즉각 삭제(`os.remove`)하도록 조치하여 WSL2 공간 누수를 완벽 차단했습니다.
* **추론 성과 평가 리포팅:** 병합 성공 직후 온디맨드 노드가 최종 모델로 추론을 수행해낸 결과(`stat.logs`)를 마스터 대시보드 및 콘솔에 스트림 중계하도록 연동했습니다.

---

## 📅 2026-07-01 패치 내역 (1차 기록 - 롤백용 보존)

### 1. [GCS/안정화] `RegisterWorker` 중복 ID 등록 세션 충돌 방지 로직 신설
* **해결 및 패치 내용:**
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)의 등록 API 내부에 중복 검증 로직을 추가했습니다. 등록 시점에 `worker_id`가 이미 등록되어 있다면 경고 로그를 남기고 실패 응답(`success=False`)을 보내어 세션 침범을 완벽히 방어하였습니다.

### 2. [인프라/스케일링] 이기종 Spot-B 워커 노드 동적 스케일링 완전 연동
* **해결 및 패치 내용:**
  - [head/cluster_manager.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/cluster_manager.py)의 `scale_out_worker()` 함수 내에 **Spot-B (0.5 Core, 512MB RAM, 시작 포트 50070)** 사양을 추가하였습니다.
  - 스케일아웃 시 `spot_a`는 `worker-2` 계열, `spot_b`는 `worker-3` 계열로 컨테이너 접두어를 분기하여 부팅 시 좀비 클린업 로직과 정합성을 맞추었습니다.
  - Spot-B 워커를 백그라운드 Eviction Daemon(강제 스팟 회수) 및 OutOfCapacity 모사 로직에 완벽히 연동하였습니다.

### 3. [자원가드/확장성] 호스트 물리 메모리 가드(Safety Guard) 상향 및 우회 변수 제공
* **해결 및 패치 내용:**
  - 스케일아웃 전 시스템 자원 상태를 검증하는 `is_host_resource_sufficient()`의 가용 메모리 임계 안전선을 기존 `2.0 GB`에서 **`4.0 GB`**로 상향하고, PC의 급격한 과부하(Swap 지연 및 VM 다운)를 사전에 차단하기 위한 해결 방법 로그를 출력하도록 개선했습니다.
  - `$env:BYPASS_RESOURCE_GUARD="1"` 환경 변수 입력 시 가용 램 점검 가드를 즉시 우회(Bypass)할 수 있는 가드 우회 옵션을 신설하여 로컬 개발 테스트 편의성을 대폭 보강했습니다.
  - [head/scheduler/core.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트\WE-MEET/head/scheduler/core.py)에 정의된 동적 스팟 워커의 최대 가동 대수 제한(`MAX_SPOT_SCALE`)을 기존 3대에서 **7대**로 확장하여 더 다이내믹한 이기종 스케일링이 가능하도록 개선했습니다.

### 4. [인프라 제어/격리] PyTorch 물리 GPU VRAM 할당 격리 제한 (VRAM Guard) 구현
* **해결 및 패치 내용:**
  - [worker/gpu_simulator.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/gpu_simulator.py) 내부 `PyTorchTaskRunner.run()` 연산 개시부 직후에 `torch.cuda.set_per_process_memory_fraction` API를 사용해 각 워커가 물리적으로 접근할 수 있는 CUDA 메모리 한도를 강제 지정하였습니다.
  - **노드 등급별 VRAM 격리 제한:** On-Demand(4.0 GB), Spot-A(2.0 GB), Spot-B(1.0 GB). 지정량 초과 점유 시 CUDA OOM 예외 발생을 유도하여 하드웨어 자원 격리 가드를 완성했습니다.

### 5. [긴급 버그 패치] 빌드 오류, 데이터셋 404 및 맵-리듀스 데드락 해결
* **NameError & ImportError 해결:** `cluster_manager.py` 내의 누락되었던 필수 표준 라이브러리(`time`, `random`, `threading`)를 바인딩하고, `gpu_simulator.py` 내 지워진 레거시 모듈에 대한 임포트 참조를 제거하여 워커/헤드 크래시를 완벽 해결했습니다.
* **MNIST 외부 404 차단 바이패스:** 외부 AWS S3 만료로 인한 torchvision 다운로드 404 실패 이슈를 해결하기 위해, 코드 내부에서 0~9 손글씨 이미지를 직접 다차원 텐서로 주입하는 **`get_inline_mnist_dataset()` 인라인 가상 데이터셋**을 구축했습니다.
* **Map-Reduce 데이터 볼륨 마운트 및 핀닝:** 격리된 컨테이너 간 가중치 전송용 도커 볼륨 마운트를 구성하고, 스팟 탈퇴로 인한 회수 방지를 위해 병합(Reduce) 연산을 `on_demand` 노드로 고정(Pinning)했습니다.
* **스케줄러 데드락 해결:** 맵 작업 분산 시 온디맨드 노드가 메인 스레드 락에 걸려 `IDLE`로 풀리지 못하던 순환 대기 데드락을 맵-리듀스 진입 시 락을 해제하도록 보강하여 완치했습니다.

### 6. [네트워크/셧다운] Graceful Shutdown 및 좀비 스팟 컨테이너 완전 소거
* **시그널 리스너 기반 자동 제거:** `Ctrl+C` 또는 `docker-compose down` 시 헤드 프로세스가 `SIGINT/SIGTERM` 시그널을 감지하여 구동 중이던 모든 동적 스팟 워커들을 도커 SDK로 즉시 동기 정리 소거하는 셧다운 핸들러를 장착했습니다.
* **외부 브릿지 네트워크 전환:** 컨테이너 정리 지연으로 네트워크 해제가 실패하던 문제를 `babyray-net` 을 external 네트워크로 매핑하여 영구 해결했습니다.

### 7. [GUI/대시보드] 파일 독립화 및 프리미엄 라이브 스케일 애니메이션 구축
* **프론트엔드 Decoupling:** `server.py` 내 하드코딩된 HTML/CSS/JS 코드를 독립 정적 웹 리소스로 완전히 물리 분리했습니다.
* **학습/SLA 지표 배너 추가:** 대시보드 상단에 스케줄러 모드, 완료/실패 수, 강화학습 탐험율(Epsilon) 실시간 현황판을 추가했습니다.
* **이원화 풀 및 스케일 애니메이션:** 온디맨드와 스팟 노드 풀을 레이아웃으로 분리하고, 노드 가입/탈퇴 시 부드러운 스케일인/아웃 페이드 트랜지션 애니메이션 효과를 구현했습니다.

---

## 📅 2026-07-01 패치 내역 (1차 기록)

### 1. [GCS/안정화] `RegisterWorker` 중복 ID 등록 세션 충돌 방지 로직 신설
* **발생 현상 & 배경:**
  - 동일한 `worker_id`를 가진 워커가 실수로 중복 기동되는 경우, 검증 없이 GCS 레지스트리의 기존 정보를 덮어씌워 통신 세션 및 모니터링 메트릭이 꼬이는 장애가 있었습니다.
* **해결 및 패치 내용:**
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)의 등록 API 내부에 중복 검증 로직을 추가했습니다. 등록 시점에 `worker_id`가 이미 등록되어 있다면 경고 로그를 남기고 실패 응답(`success=False`)을 보내어 세션 침범을 완벽히 방어하였습니다.

### 2. [인프라/스케일링] 이기종 Spot-B 워커 노드 동적 스케일링 완전 연동
* **발생 현상 & 배경:**
  - 기존 설계 파일에는 Spot-B 노드가 명시되어 있었으나, 컨테이너 관리 모듈에서 실제 Spot-B 지원이 누락되어 이기종 확장이 구동되지 못하고 있었습니다.
* **해결 및 패치 내용:**
  - [head/cluster_manager.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/cluster_manager.py)의 `scale_out_worker()` 함수 내에 **Spot-B (0.5 Core, 512MB RAM, 시작 포트 50070)** 사양을 추가하였습니다.
  - 스케일아웃 시 `spot_a`는 `worker-2` 계열, `spot_b`는 `worker-3` 계열로 컨테이너 접두어를 분기하여 부팅 시 좀비 클린업 로직과 정합성을 맞추었습니다.
  - Spot-B 워커를 백그라운드 Eviction Daemon(강제 스팟 회수) 및 OutOfCapacity 모사 로직에 완벽히 연동하였습니다.

### 3. [자원가드/확장성] 호스트 물리 메모리 가드(Safety Guard) 상향 및 스케일 상한선 상향
* **해결 및 패치 내용:**
  - 스케일아웃 전 시스템 자원 상태를 검증하는 `is_host_resource_sufficient()`의 가용 메모리 임계 안전선을 기존 `2.0 GB`에서 **`3.0 GB`**로 상향하고, PC의 급격한 과부하(Swap 지연 및 VM 다운)를 사전에 차단하기 위한 설명 주석을 보강하였습니다.
  - [head/scheduler/core.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트\WE-MEET/head/scheduler/core.py)에 정의된 동적 스팟 워커의 최대 가동 대수 제한(`MAX_SPOT_SCALE`)을 기존 3대에서 **7대**로 확장하여 더 다이내믹한 이기종 스케일링이 가능하도록 개선했습니다.


### 4. [인프라 제어/격리] PyTorch 물리 GPU VRAM 할당 격리 제한 (VRAM Guard) 구현
* **발생 현상 & 배경:**
  - 기존에는 GPU 이기종 성능의 편차를 모사하기 위해 연산 후 인위적인 시간 지연(`sleep`)만 가하였을 뿐, 물리적으로 GPU 하드웨어 자원을 제어할 수 없는 한계가 있었습니다.
* **해결 및 패치 내용:**
  - [worker/gpu_simulator.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/gpu_simulator.py) 내부 `PyTorchTaskRunner.run()` 연산 개시부 직후에 `torch.cuda.set_per_process_memory_fraction` API를 사용해 각 워커가 물리적으로 접근할 수 있는 CUDA 메모리 한도를 강제 지정하였습니다.
  - **노드 등급별 VRAM 격리 제한:** On-Demand(4.0 GB), Spot-A(2.0 GB), Spot-B(1.0 GB). 지정량 초과 점유 시 CUDA OOM 예외 발생을 유도하여 하드웨어 자원 격리 가드를 완성했습니다.

---

## 📅 2026-06-27 패치 내역

### 1. [오토스케일링/일관성] 초기 스케일 변수와 docker-compose 실기동 대수 간 정합성 에러 패치
* **발생 현상 & 배경:**
  - `docker-compose.yml`에는 Spot 워커(`worker-2`, `worker-3`)가 주석 처리되어 서비스 기동 시 실제 기동되는 대수가 **0대**였습니다.
  - 그러나 `head.py` 내의 스케줄러 루프에서는 이 스케일 초기 변수(`current_worker_2_scale`, `current_worker_3_scale`)를 `1`로 정의하여 사용하고 있었습니다.
  - 이로 인해, 스케일아웃으로 Spot 워커가 최초 1대 띄워졌을 때 변수값은 `2`가 되고, 이후 부하 감소로 스케일인이 작동할 때 변수가 `2`에서 `1`로 감소하며 유일하게 켜져 있던 1대의 Spot 워커 컨테이너를 삭제하게 됩니다.
  - 그 결과, 실제 동작하는 컨테이너는 0대임에도 변수값은 `1`로 유지되어 더 이상 최소 보장 조건(`scale > 1`)에 의해 스케일인이 정상적으로 작동하지 않고 자원 카운트의 정합성이 꼬이는 결함이 존재했습니다.
* **해결 및 패치 내용:**
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)의 초기 스케일 변수 값을 실제 구동 환경에 부합하도록 `0`으로 수정했습니다 (`current_worker_2_scale = 0`, `current_worker_3_scale = 0`).
  - 스케일인 감축 판단 기준을 `> 1`에서 `> 0`으로 완화하여, 부하가 없을 때 모든 동적 Spot 워커가 완전히 0대로 스케일인(안전 회수 및 파괴) 되도록 수식 정합성을 패치하였습니다.

### 2. [빌드/복구] `compile_proto.py` 복구 및 임포트 치환 패턴 불일치 예외 패치
* **발생 현상 & 배경:**
  - 개발 툴 오조작으로 `compile_proto.py`가 유실되어 빌드 시 exit code 2가 떴던 문제를 해결하고자 1차 복구하였으나, 새로 제너레이션된 `babyray_pb2_grpc.py` 내부의 구문(`import babyray_pb2 as babyray__pb2`)이 1차 작성한 `compile_proto.py`의 탐색 패턴(`import babyray_pb2 as proto_dot_babyray__pb2`)과 불일치하여 패치가 누락되는 상황이 발견되었습니다. 이로 인해 컨테이너 구동 시 `ModuleNotFoundError: No module named 'babyray_pb2'` 크래시가 발생했습니다.
* **해결 및 패치 내용:**
  - [compile_proto.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/compile_proto.py)를 전면 보강하여 두 가지 패턴(`import babyray_pb2 as babyray__pb2` 및 `import babyray_pb2 as proto_dot_babyray__pb2`)을 모두 탐색해 유연하게 치환하도록 개선했습니다.
  - 이를 통해 컨테이너 내부에서도 모듈 탐색 오류 없이 GCS 및 워커 통신이 완벽하게 초기화되도록 조치했습니다.

### 3. [시뮬레이터/부하 강화] 스케일링 검증을 위한 난수 워크로드 강화 및 스케줄러 의사결정 빈도 단축 (큐 적재 해결)
* **발생 현상 & 배경:**
  - 기존 부하 모델은 4초당 1~3개 유입되도록 부하를 올렸으나, 스케줄러 루프(`scheduler_loop`)의 주기가 **4초**에 1회씩 돌면서 대기열에서 한 번에 1개씩만 태스크를 분배했습니다.
  - 이로 인해 유입 빈도(평균 2.4개/4초)에 비해 분배율(최대 1개/4초)이 낮아, 가용 IDLE 워커가 10대나 있고 스케일이 최대치에 도달했음에도 대기 큐 적재(병목 정체)가 심하게 지속되었습니다.
* **해결 및 패치 내용:**
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)의 스케줄러 의사결정 루프 주기를 기존 4.0초에서 **1.0초**로 파격적으로 단축했습니다.
  - 의사결정 빈도를 4배로 끌어올림에 따라 유입되는 태스크를 실시간으로 빠르게 워커들에 디스패치(분배)하여 큐 적재 정체 현상을 깔끔하게 해결했습니다.
  - 이와 연동하여 룰 기반 스케일인 타이머(`empty_queue_duration`)의 시간 증가 단위를 `+ 4.0`에서 `+ 1.0`으로 보정하여 정합성을 유지했습니다.

### 4. [오토스케일링/모니터링] 순차 네이밍(인덱스 재사용) 적용 및 어지러운 `@` ID 접미사 전면 삭제
* **발생 현상 & 배경:**
  - 기존의 동적 Spot 워커들은 `worker-2-4206`과 같이 무작위 4자리 난수 네이밍을 지정하고 기동하였으며, GCS 내 중복 식별 방지를 위해 `worker-2-1@fa5a067dda4e` 처럼 컨테이너 ID를 `@` 뒤에 꼬리표로 엮어서 사용했습니다.
  - 이 방식은 콘솔 모니터링 로그를 어수선하게 만들 뿐 아니라 가독성과 직관성을 떨어뜨리는 한계가 있었습니다.
* **해결 및 패치 내용:**
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)의 `scale_out_worker`를 개편하여 현재 가동 중인 워커들의 인덱스를 스캔해 비어 있는 가장 작은 순차 인덱스(1번부터 시작)를 부여하고, 스케일인으로 소멸한 번호는 우선 재활용(Index Recycling)하도록 정교화했습니다.
  - 순차 명칭(`worker-2-1` 등) 자체로 고유 식별이 완전 보장되므로 [worker/worker.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/worker.py)에서 `@socket.gethostname()` 접미사를 전면 삭제했습니다.
  - Head Node의 `SendHeartbeat` 및 `scale_in_specific_worker`에서도 `@` 파싱 코드를 소거하고, `f"babyray-{worker_id}"` 명명 규칙을 통해 Docker SDK가 직접 컨테이너를 식별 및 제어하도록 아키텍처를 간결하게 가다듬었습니다.

### 5. [자원가드/안정성] Head 가동 시 호스트 잔존 좀비 컨테이너 일괄 청소 (Startup Cleanup)
* **발생 현상 & 배경:**
  - 사용자가 분산 시스템을 Ctrl+C 등으로 비정상 종료 시, Docker SDK를 통해 호스트에 독립 기동한 Spot 워커 컨테이너들은 함께 자동 종료되지 않고 좀비 상태로 유지되었습니다.
  - 이는 호스트의 메모리 부족(0.81 GB로 잠식) 현상을 유발하여 다음 테스트 기동 시 스케일아웃 기동을 전면 마비시키는 원인이 되었습니다.
* **해결 및 패치 내용:**
  - Head 노드가 가동될 때 호스트 상의 이전 사이클 동적 워커 컨테이너들을 찾아내 소거하는 `cleanup_zombie_containers()` 메소드를 구현하고, gRPC 서버의 부팅 속도를 블로킹하지 않도록 `serve()` 상단에서 **비동기 데몬 스레드**로 구동되도록 처리했습니다.
  - 이를 통해 기동 시마다 좀비 컨테이너들을 깨끗하게 일괄 정지/삭제(자원 회수)하여 호스트 메모리 누수를 원천 방어하고 부팅 지연 현상을 완벽히 해소했습니다.

### 6. [자원가드/성능] 사용자 물리 자원 확보 피드백 기반 메모리 임계치 2.0 GB로 상향 보정
* **발생 현상 & 배경:**
  - 호스트 자원 가드의 가용 메모리 경계선이 1.0 GB로 작동할 때, 사용자 컴퓨터의 가용 메모리가 1.0 GB 미만으로 밀려나면서 스케일아웃이 계속 안전 거부되어 교착상태에 빠졌습니다.
* **해결 및 패치 내용:**
  - 사용자가 호스트 시스템의 2.0 GB 이상 가용 공간을 책임지고 항상 확보하는 튜닝 피드백을 적용함에 따라, [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py) 내 `is_host_resource_sufficient()`의 차단 임계치를 `2.0 GB`로 상향 보정하여, 2GB 이상의 가용 메모리가 확보된 상태에서 오토스케일링이 강력하고 안정적으로 작동하도록 일치시켰습니다.

### 7. [오토스케일링/RL] Spot 워커 단일화 및 30대 확장 탑재 (Spot-B 제거, Spot-A 통합 30대 확장)
* **발생 현상 & 배경:**
  - Spot-A(`worker-2-x`)와 Spot-B(`worker-3-x`)의 비대칭 노드 구조는 복잡성을 더했고, 최대 8대 한도로 대용량 난수 큐 작업을 고속으로 병렬 소화하기에는 스케일 제한이 엄격했습니다.
* **해결 및 패치 내용:**
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)에서 `Spot-B (worker-3)` 설정을 완전 폐기하고 **`Spot-A (worker-2)` 단일 계열로 스팟 워커를 단일화**했습니다.
  - 스케일링 상한선을 **최대 30대**로 대폭 상향하여, 부하 조건에 맞추어 `worker-2-1`부터 `worker-2-30`까지 거침없이 병렬 확장되도록 개방했습니다.
  - active bitmap을 2비트로 압축해 강화학습 수렴 정밀도를 끌어올렸습니다.

### 8. [스케줄링/아키텍처] 1틱 다중 분배 (Multi-Dispatch) 아키텍처 개편 (큐 적재 근본 조치)
* **발생 현상 & 배경:**
  - 큐의 읽기/쓰기는 Head의 단일 스케줄러 스레드가 독점 제어하지만, 1틱(1초 주기)에 단 1개의 태스크만 꺼내서 배정하는 **Single-Dispatch 한계** 때문에, IDLE 워커가 8대 이상 노닐고 있어도 큐가 즉시 비워지지 않고 적재 정체가 고질적으로 발생하는 원인이 되었습니다.
* **해결 및 패치 내용:**
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)의 `scheduler_loop` 내부 의사결정 로직을 `while True` 서브 루프로 감싼 **다중 배정 (Multi-Dispatch) 엔진**으로 전면 개편했습니다.
  - 대기 큐에 작업이 존재하고 가용한 IDLE 워커가 존재하는 동안 **단 1초 만에 연속 루프를 돌며 가용 워커 전원에 태스크를 마구 퍼부어 할당**하도록 개선했습니다.
  - 더 이상 가용 워커가 없거나 HOLD, SCALE_OUT이 트리거되면 서브 루프를 탈출하여 다음 틱으로 넘기도록 안전망을 구축하여 큐 적재를 제로화했습니다.

---

## 📅 2026-06-26 패치 내역

### 1. [아키텍처/오토스케일링] Docker SDK 기반 동적 클러스터 스케일링 전면 개편 (멘토 피드백 2-1 반영)
* **발생 현상 & 배경:**
  - 기존에는 `docker compose --scale` 명령을 사용하여 스팟 워커들의 활성 대수를 변경하고 있었습니다.
  - 이 경우, 축소(Scale-In)가 일어날 때 Docker가 임의의 컨테이너를 종료하므로 **실제 작업을 수행 중인(BUSY) 컨테이너가 중단되는 작업 유실 위험**이 있었으며, 형상관리가 무작위화되는 한계가 존재했습니다.
* **해결 및 패치 내용:**
  - `docker-compose.yml` 서비스 정의에서 스케일링 대상인 `worker-2` 및 `worker-3` 설정을 완전히 비활성화(주석 처리)하여 정적 배포 영역과 동적 스케일링 영역을 분리했습니다.
  - Head 노드의 [head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)에 Docker SDK(`DOCKER_CLIENT.containers.run` 및 `containers.get`)를 직접 호출하는 `scale_out_worker(node_type)` 및 `scale_in_specific_worker(node_type)`를 새로 구현했습니다.
  - **안전한 Scale-In (타겟별 회수):** 스케일인 시 GCS `worker_registry`에서 상태가 `"IDLE"`인 워커만 골라내어 고유 컨테이너 ID를 파싱한 뒤, Docker SDK로 해당 컨테이너만 지정 정지 및 제거(`container.stop` 및 `remove`)하도록 조치하여 태스크 무중단 가용성을 확보했습니다.

---

### 2. [모니터링/자원 가드] 호스트 시스템 물리 자원 관리 레이어 구축 (멘토 피드백 2-4 반영)
* **발생 현상 & 배경:**
  - 클러스터 전체 및 호스트의 물리 자원 한계치를 감지하지 못하고 무조건적인 스케일아웃을 감행할 경우, 호스트 PC 자체가 자원 부족(OOM)으로 크래시가 발생할 수 있는 위험이 있었습니다.
* **해결 및 패치 내용:**
  - Head 노드 내에 호스트 시스템의 물리 자원 상황을 감시하는 보호막 함수 `is_host_resource_sufficient()` 및 `get_gpu_free_memory()`를 추가했습니다.
  - `psutil`을 활용해 호스트의 가용 메모리가 **1.0 GB 미만**이거나, `nvidia-smi` 덤프를 통해 가용 GPU VRAM이 **500 MiB 미만**인 경우, 스케일아웃(`scale_out_worker`)을 사전에 차단하고 예외 보호 로그를 출력하는 **Global Resource Guard** 레이어를 완성했습니다.

---

### 3. [고가용성/결함 주입] 가상 OOM 이상 상황 모사 및 선제 회피 (Failure Injection & Avoidance) (멘토 피드백 2-2 반영)
* **발생 현상 & 배경:**
  - 실제 개발 장비의 하드웨어를 강제로 셧다운하거나 물리 OOM을 터뜨려 테스트하는 것은 장비 수명과 안전성에 위험하므로, 소프트웨어적으로 이상 징후를 모사하고 극복하는 모의 결함 테스트베드가 필요했습니다.
* **해결 및 패치 내용:**
  - [gpu_simulator.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/gpu_simulator.py) 및 [worker.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/worker.py) 내부에 **가상 OOM Failure Injector**를 이식했습니다.
  - `"LSTM"` 연산 작업 수행 시 15%의 확률로(혹은 태스크 ID에 `fail` 문구 포함 시) 가상 OOM을 강제 유발하고 작업 상태를 `FAILED`로 보고하며, 하트비트 시 자원 점유율을 **99.9%**로 속여서 송신하도록 구현했습니다.
  - **OOM 선제 회피 제어:** Head 노드에서 워커에 태스크를 배정할 때, GCS 정보 상 메모리가 **90% 이상** 점유된 노드는 자동으로 선택지에서 배제하도록 구현하여 이상 워커로의 할당을 선제적으로 예방했습니다.
  - **자가 자원 회수 연동:** 과부하/오버로드된 IDLE 워커는 룰 기반 Scale-In 루프에서 **1순위 감축 대상**으로 자동 필터링되어 호스트로부터 신속하게 삭제 정리되도록 자가 치유 라이프사이클을 연계했습니다.

---

### 4. [오토스케일링] 룰 기반(Rule-based) Scale-In 감지 루프 및 타이머 설계 (멘토 피드백 2-3 반영)
* **발생 현상 & 배경:**
  - 기존 Q-learning 에이전트는 스케일아웃만 선택할 수 있었고, 스케일인의 부재로 인해 한 번 늘어난 컨테이너들이 영원히 소멸되지 않아 비용 인지에 역행했습니다.
* **해결 및 패치 내용:**
  - [head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py)의 `scheduler_loop()` 내부에 대기열 크기가 0개이고 평균 CPU 사용률이 20% 미만인 상태를 체크하는 타이머 `empty_queue_duration`을 탑재했습니다.
  - 해당 저부하 상태가 **10.0초 이상 지속**될 경우, 기동 중인 Spot 워커를 1대씩 순차적으로 감축(`scale_in_specific_worker`)하고 1대 초과분을 자동으로 회수하도록 룰 기반 탄력 오토스케일러를 연동시켰습니다.

---

## 📅 2026-06-25 패치 내역

### 1. [네트워크] Worker 컨테이너의 Head 노드 연결 실패 이슈
* **발생 현상:**
  ```text
  babyray-worker-1  | [Heartbeat] Head 서버 연결 시도: localhost:50051...
  babyray-worker-1  | [Heartbeat] Head 서버 연결 지연. 3초 후 재시도...
  ```
* **원인 분석:**
  - Docker Compose 가상 브리지 네트워크 환경에서 `localhost` 혹은 `127.0.0.1`은 호스트 PC나 타 컨테이너가 아니라 **Worker 컨테이너 자기 자신**을 가리킵니다.
  - 따라서 Worker 노드는 자기 자신 내부의 50051 포트로 gRPC 연결을 시도하게 되어 통신 실패(연결 지연)가 무한 반복되었습니다.
* **해결 및 패치 내용:**
  - `docker-compose.yml` 서비스 정의에 기재된 `HEAD_HOST=head` 및 `HEAD_PORT=50051` 환경 변수를 Worker가 정상적으로 읽어오도록 [worker.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/worker.py) 최하단 `argparse` 기본값을 패치했습니다.
  ```python
  # 기존 코드
  parser.add_argument("--head-host", type=str, default="localhost")
  
  # 수정 코드 (환경 변수를 우선적으로 참조하도록 바인딩)
  parser.add_argument("--head-host", type=str, default=os.environ.get("HEAD_HOST", "localhost"))
  ```

---

### 2. [인프라 제어] Head 노드 내부 Docker CLI 명령어 부재 이슈
* **발생 현상:**
  ```text
  babyray-head      | [Scheduler Action] SCALE_OUT 트리거 -> worker-2 (Spot-A) 대수 증설 지시 (2대)
  babyray-head      | [Docker SDK CLI 에러] worker-2 스케일링 실패: [Errno 2] No such file or directory: 'docker'
  ```
* **원인 분석:**
  - Head Node의 스케줄러는 부하 상황 감지 시 `subprocess`를 통해 `docker compose` 명령을 직접 내려 컨테이너를 동적으로 스케일링하도록 구현되어 있습니다.
  - 그러나 베이스 이미지(`pytorch/pytorch`)는 PyTorch 구동에 특화된 런타임 이미지이므로, 컨테이너 내부에 `docker` 클라이언트 툴이나 `compose` 플러그인이 깔려 있지 않아 명령 실행 자체가 실패했습니다.
* **해결 및 패치 내용:**
  - [docker/Dockerfile](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/docker/Dockerfile) 빌드 명령어 스펙에 초경량 static `docker-cli` 패키지 및 `docker-compose` v2 플러그인을 직접 다운로드하여 설치하는 레이어를 추가했습니다.
  ```dockerfile
  # Docker CLI 및 Docker Compose CLI 플러그인 빌드 타임 자동 설치
  RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-24.0.7.tgz | tar -xz -C /tmp \
      && mv /tmp/docker/docker /usr/local/bin/ \
      && rm -rf /tmp/docker
  RUN mkdir -p /usr/local/lib/docker/cli-plugins \
      && curl -SL https://github.com/docker/compose/releases/download/v2.24.5/docker-compose-linux-x86_64 -o /usr/local/lib/docker/cli-plugins/docker-compose \
      && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
  ```

---

### 3. [런타임] 컨테이너 실행 환경의 임포트 경로 에러 (`sys.path`)
* **발생 현상:**
  ```text
  babyray-head  | ModuleNotFoundError: No module named 'q_learning'
  babyray-worker-2  | ModuleNotFoundError: No module named 'gpu_simulator'
  ```
* **원인 분석:**
  - Docker 컨테이너 구동 시 작업 디렉토리(`/app`)를 루트로 하여 모듈을 실행(`python -m head.head`)하므로 파이썬의 `sys.path` 상단에는 `/app`만 들어가게 됩니다.
  - 이에 따라 `head/head.py` 내부에서 같은 디렉토리의 `q_learning.py`를 `from q_learning import ...` 형태로 임포트할 때 경로를 탐색하지 못하는 패키지 격리 에러가 발생했습니다.
* **해결 및 패치 내용:**
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py) 및 [worker/worker.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/worker.py) 최상단에 현재 실행 중인 파일의 절대 경로 폴더를 `sys.path`에 추가하도록 조치했습니다.
  ```python
  sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))) # 루트 경로
  sys.path.append(os.path.abspath(os.path.dirname(__file__)))                    # 개별 패키지 경로 추가
  ```

---

### 4. [인프라 제어/고가용성] Head 컨테이너 무한 재구성 및 중단 이슈 (DooD 재귀 루프)
* **발생 현상:**
  - Auto-Scaling 시점에 Head 컨테이너가 갑자기 종료되고 `exited with code 137`이 발생하며, Worker들이 `경고: 생존 신고 전송 실패`를 무한히 출력하고 연결을 체결하지 못했습니다.
* **원인 분석:**
  - Head 노드 내부에서 `docker compose up -d --scale` 명령을 실행할 때, Docker Compose가 호스트 상에 구동 중인 전체 컨테이너 세트를 검사하여 형상 일치 여부를 판별합니다.
  - 이 과정에서 Docker Compose가 `babyray-head` 컨테이너의 최신 사양이 맞지 않거나 업데이트가 필요하다고 오판하여 **자기 자신(`babyray-head`)을 죽이고 재생성(Recreate)** 하였습니다.
  - 이로 인해 Head 프로세스는 종료(137)되고, 새로 시작된 Head는 기존 GCS 레지스트리를 잃은 채 기동 $\rightarrow$ 또다시 스케일아웃 발생 $\rightarrow$ 자기 자신을 다시 죽이고 재생성하는 **무한 재귀 OOM/137 루프**에 빠졌습니다.
* **해결 및 패치 내용:**
  - [head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py) 내부 `scale_workers` 함수 내의 Docker Compose 실행 인자값에 `--no-recreate` 옵션을 추가하고, 마지막 타겟으로 특정 `service_name`(예: `worker-2`)만 지정하도록 수정하여 Head 컨테이너를 건드리지 않도록 차단했습니다.
  ```python
  cmd = [
      "docker", "compose",
      "-f", compose_path,
      "up", "-d",
      "--no-recreate",
      "--scale", f"{service_name}={target_count}",
      service_name
  ]
  ```

---

### 5. [인프라 제어/확장성] 동적 스케일아웃 적용 시 컨테이너명 충돌 및 중복 등록 이슈 (Auto Scaling 확장성 패치)
* **발생 현상:**
  - `docker compose --scale` 실행 시 고정된 컨테이너명 설정으로 인해 스케일링이 차단되거나, 여러 대 구동 시 동일한 ID(`worker-2`)로 헤드 노드에 중복 등록되어 기존 세션 정보를 덮어쓰고 통신이 어긋나는 이슈가 있었습니다.
* **원인 분석:**
  - `docker-compose.yml` 내부에 `container_name: babyray-worker-2`와 같이 고정된 컨테이너명이 설정되어 있으면, Docker Compose는 이를 2대 이상으로 확장하여 띄울 수 없습니다 (이름 충돌 방지 차원).
  - 또한, `--id worker-2` 옵션으로 실행되는 모든 스케일링 인스턴스들이 동일한 ID로 헤드에 `RegisterWorker`를 호출하여 레지스트리 맵의 키를 덮어쓰는 문제가 있었습니다.
* **해결 및 패치 내용:**
  - [docker/docker-compose.yml](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/docker/docker-compose.yml) 파일에서 스케일링 대상인 `worker-2` 및 `worker-3` 서비스의 고정 `container_name` 설정을 제거하여 도커가 고유 번호 기반의 다중 컨테이너를 가동할 수 있도록 허용했습니다.
  - [worker/worker.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/worker.py) 최하단 구동부에서 `socket.gethostname()` (컨테이너 ID)을 추출하여 기존 ID 뒤에 `@` 구분자로 결합함으로써 고유 ID를 보장했습니다 (`worker-2@<container_id>`).
  - [head/head.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/head.py) 내부 `SendHeartbeat`에서 이 `@` 구분값에서 컨테이너 ID를 파싱하여 도커 SDK의 개별 리소스 메트릭을 추적하도록 처리하고, 스케줄러 루프에서 특정 ID가 아닌 `node_type`과 `IDLE` 상태 기준으로 가용한 워커를 탐색·할당하도록 범용성을 패치했습니다.

---

### 6. [모니터링] `psutil` 및 `cgroups` 연동을 통한 실시간 실제 자원 사용량 리포팅 패치
* **발생 현상:**
  - 기존에는 Worker 노드 기동 시 Heartbeat 전송부에서 실시간 점유율이 아닌 하드코딩된 더미 메트릭 값(`cpu=12.5%`, `mem=40.0%`)을 송신하고 있었습니다.
* **원인 분석:**
  - 로컬 노드 기동 및 컨테이너 환경의 격리 시 실제 연산 부하가 스케줄러로 피드백되지 않아 Q-learning 에이전트의 상태(State) 판단에 왜곡이 생길 수 있었습니다.
* **해결 및 패치 내용:**
  - [worker/worker.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/worker/worker.py) 최상단에 `psutil` 라이브러리를 임포트하고, 하트비트 루프 기동 전 CPU 수집 캘리브레이션을 수행하도록 구현했습니다.
  - 리눅스 컨테이너 격리 메모리(cgroup v1/v2)를 우선 탐색하는 경로 파싱 로직(`memory.usage_in_bytes`, `memory.current` 등)을 탑재하여 격리 제한 대비 실제 사용 비중을 산출하고, 예외 발생 시 `psutil.virtual_memory().percent`로 자동 폴백(Fallback) 처리하여 gRPC 통신으로 실시간 데이터를 전송하도록 연동을 완수했습니다.

---

## 📅 2026-06-24 패치 내역

### 1. Protobuf 컴파일 외부 임포트 시 경로 불일치 이슈
* **발생 현상:**
  - `grpc_tools.protoc` 컴파일러가 생성한 `babyray_pb2_grpc.py` 내부에 `import babyray_pb2 as babyray__pb2`가 선언되어, 외부 모듈에서 `import proto.babyray_pb2_grpc` 형태로 패키지 접근 시 의존 관계가 깨져 임포트가 실패했습니다.
* **해결 및 패치 내용:**
  - [compile_proto.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/compile_proto.py) 내부 컴파일 완료 코드 블록에 임포트 경로 자동 치환(Patch) 논리를 적용하여 컴파일 직후 파일 내 `import babyray_pb2` 구문을 `from proto import babyray_pb2`로 문자열 치환 패치하도록 수정하여 모듈 구조를 정상화시켰습니다.

## 📅 2026-07-02 패치 내역

### 1. 호스트 물리 램 85% 점유율 임계 가드 개편 및 Eviction Freeze 안전장치 탑재
* **패치 내용:**
  - `is_host_resource_sufficient()`의 물리 램 가용량 하한선(4.0 GB) 점검 방식을 **'호스트 물리 램 전체 점유율 85.0% 상한선 검사'**로 전면 전환했습니다.
  - 임의 스팟 강제 회수 검사 주기(`time.sleep`)를 기존 25초에서 타협안인 **`10.0초`**로 타이트하게 조율했습니다.
  - 호스트 물리 램이 85.0%를 초과하여 신규 노드 증설(Scale-out)이 차단된 경우, 자원 기아(Starvation) 및 기동 중인 워커 몰살을 막기 위해 **Eviction Daemon의 강제 회수 동작을 즉시 일시 동결(Eviction Freeze)**하도록 상호 안전 가드를 엮었습니다.

### 2. Map-Reduce 점진적 맵 캐싱 및 선택적 재시도(Selective Retry) 파이프라인 탑재
* **패치 내용:**
  - 맵-리듀스 연산 시 스팟 노드가 임의 회수되거나 실패해도 전체 에포크를 강제 롤백하지 않고, **이미 성공한 맵 인덱스 조각(SUCCESS)의 가중치 결과물은 보존(캐싱)**하도록 수정했습니다.
  - 실패하거나 누락된 특정 맵 인덱스 조각(`pending_indices`)만 필터링하여 가용 워커에 선별 위임하고 반복해서 점진적 완성하는 **선택적 재시도(Selective Map Retry) 루프**를 구축했습니다.
  - 맵 워커 배정 우선순위 정렬 시, 절대로 회수되지 않는 고신뢰성 온디맨드 노드(`worker-1`)가 **리스트의 가장 앞(Index 0)에 위치하도록 강제 우선순위(Pinning) 처리**하여 연산 안전성을 보증했습니다.

### 3. 분산 AI 결론 결합 엔진 및 실시간 Conclusions UI 패널 신설
* **패치 내용:**
  - 분산 맵-리듀스/단일 추론 연산이 완료되었을 때, 각 모델(CNN/RNN/LSTM)의 예측 성과를 최종적으로 결합하는 **분산 AI 결론 결합 엔진**을 `utils.py`에 이식했습니다.
    - **CNN:** 다수결 투표(Majority Voting) 및 평균 신뢰도 결합
    - **RNN:** 분산 수치 예측의 FedAvg 스타일 산술 평균 결합
    - **LSTM:** 생성된 개별 문장 프래그먼트 병합
  - 대시보드 GUI 하단에 고유 모델 색상 테두리를 지닌 Premium Glassmorphism 스타일 **Conclusions UI 전용 리스트 패널**을 추가하여, 완료 시점에 병합 예측 결론이 즉시 동적 렌더링되도록 구현했습니다.

### 4. Spot-B(worker-3) 노드 복원 및 Q-Learning 4D 상태/6대 행동 고도화
* **패치 내용:**
  - `docker-compose.yml` 에 주석처리되어 있던 **Spot-B 노드(0.5 Cores, 512M memory, $0.2/hour)**를 정식 복원하고, `cluster_manager.py` 스케일아웃에 512MB RAM 물리 한도 격리 규칙을 동기화 가동했습니다.
  - 요금 위험($P_{trend}$)과 잔여 예산($B_{level}$)을 하나의 직관적인 **가용 예산-가격 비율 지수($B_{avail}$)**로 단일화 통폐합했습니다.
  - 스케줄링 의사결정 시 예산 소모 대 가속의 무게추를 정밀 제어하도록 **SLA 마감 임박성($U_{SLA}$, 30초 임계선)** 차원을 신설하여 4차원 상태 공간 $State = (W\_mix, A\_mix, U\_SLA, B\_avail)$로 정밀 리팩토링했습니다.
  - OD 배정(0), Spot-A 배정(1), Spot-B 배정(2), HOLD(3), Spot-A 증설(4), Spot-B 증설(5)의 **6대 행동 공간**으로 확장했습니다.
  - 고속 오프라인 가상 사전 학습 시뮬레이터(`pretrain.py`)를 개발하여, 에포크 25,000회 고속 훈련을 마쳐 완벽하게 수렴된 Q-Table 가중치(`q_table.json`)를 번들 탑재(기동 시 Epsilon=0.05로 즉각 지능 스케줄링 가동)시켰습니다.

### 5. 공유 볼륨 네임스페이스 격리 버그 픽스 및 .pt 가중치 병합 연동 보증
* **발생 현상:**
  - 동적 스케일아웃으로 기동되는 Spot 컨테이너들은 `cluster_manager.py`에 의해 물리 `"babyray-data"` 볼륨을 마운트하고 있었으나, docker-compose로 띄워진 head 및 온디맨드 워커는 디렉토리 접두사가 자동 합성된 `"docker_babyray-data"` 물리 볼륨을 사용하는 불일치(Volume Mismatch) 현상이 발생했습니다.
  - 이로 인해 스팟 워커들이 저장해 놓은 `data/*.pt` 맵 가중치 파일들을 온디맨드 노드가 찾지 못해 **`[FedAvg Error] 병합할 유효한 가중치 파일(.pt)이 디바이스상에 하나도 존재하지 않습니다.`** 에러를 무한정 터뜨렸습니다.
* **해결 및 패치 내용:**
  - `docker-compose.yml` 볼륨 정의 블록 내에 `name: babyray-data`를 명시하여 접두사 합성을 배제하고 순수한 절대 볼륨명으로 호스트에 생성/연동되도록 강제했습니다.
  - 이를 통해 모든 동적/정적 온디맨드 및 스팟 노드들이 물리적으로 100% 동일한 공유 저장공간을 바라보게 유도하여 `.pt` 파일 공유 및 FedAvg 병합 실패 문제를 완전히 해결했습니다.


