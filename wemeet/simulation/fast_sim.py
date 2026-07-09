import os
import sys
import random
import time
import yaml
from datetime import datetime, timedelta

# 프로젝트 루트 디렉토리를 path에 추가하여 wemeet 패키지 임포트 지원
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from wemeet.simulation.failure_simulator import FailureSimulator
from wemeet.config import env_config as _ec
from wemeet.learning.agent import QLearningAgent
import wemeet.learning.state_features as state_features
import wemeet.learning.reward_policy as reward_policy

# --- 가상 인프라 및 단가 설정 (유일 진실: cost_model.yaml + sim_env.yaml, env_config 로더) ---
_NODES = _ec.nodes()
_WL = _ec.workload()
_ET = _ec.exec_time()
_SP = _ec.scheduler_policy()

NODES_CONFIG = {
    n: {"cost_per_hour": _NODES[n]["cost_per_hour"],
        "gpu_scale": _NODES[n]["gpu_scale_factor"],
        "memory": _NODES[n]["memory_limit_mb"]}
    for n in ("on_demand", "spot_a", "spot_b")
}

MAX_SPOT_SCALE = _ec.scaling()["max_spot_scale_floor"]
BUDGET_LIMIT = _ec.budget()["initial_virtual_budget"]
EVICTION_POLL_SEC = _ec.failure()["eviction"]["poll_sec"]
BOOT_SEC = _ec.scaling()["boot_sec"]

# 산출물 디렉토리 (스크립트 위치 기준 절대경로 — CWD 독립)
from wemeet.config import paths as _paths
DATA_DIR = _paths.DATA_DIR  # 경로 해석기로 일원화(CWD·파일깊이 무관)

# CSV 컬럼 스키마 (visualize_benchmarks.py / analyze_details.py 와 동일)
CSV_COLUMNS = ["timestamp", "mode", "task_id", "model_type", "status",
               "execution_time", "cost", "delay",
               "od_count", "spot_a_count", "spot_b_count", "budget"]

# --- 가역적 워커 클래스 ---
class SimulatedWorker:
    def __init__(self, worker_id, node_type):
        self.worker_id = worker_id
        self.node_type = node_type
        self.status = "IDLE"  # IDLE, BUSY, BOOTING
        self.boot_time_left = 0.0
        self.active_task = None
        self.task_end_time = 0.0
        
        # cGroup 동적 리사이징 상태
        self.current_memory_limit = NODES_CONFIG[node_type]["memory"]

class FastSimulator:
    def __init__(self, mode, q_learning_training=False, seed=42, scenario=None):
        """
        Args:
            mode: "static" / "dynamic" / "q_learning"
            q_learning_training: True면 온라인 학습(Q-업데이트) 수행.
            seed: 부하 재현용 난수 시드. 여러 시드로 반복하면 통계 신뢰성(평균±편차)을 얻는다.
            scenario: sim_env.yaml scenarios 프리셋 dict(오버라이드). None이면 기본 부하.
                      허용 키: initial_virtual_budget, burst_prob, normal_prob, eviction_mult.
        """
        self.mode = mode
        self.q_learning_training = q_learning_training
        self.seed = seed

        # --- 시나리오 오버라이드 (없으면 sim_env 기본값) ---
        sc = scenario or {}
        self.virtual_budget = sc.get("initial_virtual_budget", BUDGET_LIMIT)
        self.burst_prob = sc.get("burst_prob", _WL["burst_prob"])
        self.normal_prob = sc.get("normal_prob", _WL["normal_prob"])
        self.eviction_mult = sc.get("eviction_mult", 1.0)

        self.tick_rate = 0.2  # 0.2초 단위 시뮬레이션
        self.current_time = 0.0
        self.eviction_timer = 0.0
        self.scale_in_timer = 0.0

        # 워커 레지스트리 초기화 (기본 On-Demand 1대 탑재)
        self.workers = {"worker-1": SimulatedWorker("worker-1", "on_demand")}
        self.worker_counter = 1

        # 실시간 모사 태스크 카운터
        self.total_generated_tasks = 0
        self.task_queue = []

        # 동일 부하 재현을 위해 시드 고정. 학습(training) 시에는 매 에피소드 부하가 달라야 하므로
        # 시드를 고정하지 않는다(기존 동작 유지).
        if not (self.mode == "q_learning" and self.q_learning_training):
            random.seed(self.seed)

        self.completed_tasks = []
        self.running_tasks = {}  # Q-learning 지연 보상 트래킹용
        self.task_lineage = {}  # Map-Merge용
        self.logs = []
        self.q_agent = None

    def get_simulated_timestamp(self):
        base_time = datetime(2026, 7, 9, 12, 0, 0)
        sim_time = base_time + timedelta(seconds=self.current_time)
        return sim_time.strftime("%Y-%m-%d %H:%M:%S")

    def get_current_spot_scale(self):
        return sum(1 for w in self.workers.values() if w.node_type in ["spot_a", "spot_b"])

    def scale_out_worker(self, node_type):
        # OutOfCapacity 시뮬레이션
        if FailureSimulator.check_out_of_capacity(node_type):
            return False
        
        self.worker_counter += 1
        worker_id = f"worker-{self.worker_counter}"
        worker = SimulatedWorker(worker_id, node_type)
        worker.status = "BOOTING"
        worker.boot_time_left = BOOT_SEC  # 스팟 콜드스타트 부팅 시간 모사
        self.workers[worker_id] = worker
        return True

    def scale_in_worker(self, node_type):
        # IDLE 상태인 워커 중 선별 소거
        idle_candidates = [w for w in self.workers.values() if w.node_type == node_type and w.status == "IDLE"]
        if idle_candidates:
            del self.workers[idle_candidates[0].worker_id]
            return True
        return False

    def check_eviction_daemon(self):
        # 10초 주기로 회수 심사
        p_spot = FailureSimulator.current_danger_phase(self.current_time)
        # 시나리오 회수 배율 적용(상한 1.0). high_eviction 등에서 변동성 심화 모사.
        preemption_probs = {k: min(1.0, v * self.eviction_mult) for k, v in _ec.preemption_probs().items()}

        spot_workers = [w for w in self.workers.values() if w.node_type in ["spot_a", "spot_b"] and w.status != "BOOTING"]
        for w in spot_workers:
            if FailureSimulator.check_eviction(w.node_type, p_spot, preemption_probs):
                self.handle_node_death(w.worker_id, was_evicted=True)

    def handle_node_death(self, worker_id, was_evicted=True, was_oom=False):
        worker = self.workers.get(worker_id)
        if not worker:
            return
            
        # 1. 실행 중이던 태스크 회수 실패 처리 및 재큐잉
        if worker.active_task:
            task = worker.active_task
            task["attempts"] += 1
            status_str = "FAILED"
            
            # 메트릭 로깅
            exec_time = self.current_time - task["submit_time"] if not was_oom else 0.0
            cph = NODES_CONFIG[worker.node_type]["cost_per_hour"]
            task_cost = cph * (exec_time / 3600.0)
            delay = max(0.0, self.current_time - task["deadline"])
            
            self.logs.append([
                self.get_simulated_timestamp(),
                self.mode,
                task["task_id"],
                task["model_type"],
                status_str,
                round(exec_time, 4),
                round(task_cost, 6),
                round(delay, 4),
                sum(1 for wk in self.workers.values() if wk.node_type == "on_demand"),
                sum(1 for wk in self.workers.values() if wk.node_type == "spot_a"),
                sum(1 for wk in self.workers.values() if wk.node_type == "spot_b"),
                round(self.virtual_budget, 6)
            ])
            
            # --- Q-Learning 피드백 업데이트 ---
            if self.mode == "q_learning" and self.q_agent and task["task_id"] in self.running_tasks:
                binding = self.running_tasks[task["task_id"]]
                state = binding["state"]
                action = binding["action"]
                
                reward = self.q_agent.calculate_reward(
                    success=False,
                    execution_time=exec_time,
                    worker_type=worker.node_type,
                    delay_time=delay,
                    deadline_exceeded=self.current_time > task["deadline"],
                    current_model=task["model_type"],
                    co_scheduled_models=[],
                    evicted=was_evicted,
                    oom=was_oom
                )
                
                if self.q_learning_training:
                    next_state = self.get_current_state_key()
                    self.q_agent.update_q_value(state, action, reward, next_state)
                    self.q_agent.save_q_table()
                
                del self.running_tasks[task["task_id"]]

            if task["attempts"] < 3:
                # 큐 선두로 긴급 롤백
                self.task_queue.insert(0, task)
                
        # 2. 레지스트리 소거
        del self.workers[worker_id]

    def run_static_scheduler(self):
        # Static: FIFO + On-Demand 우선
        q_len = len(self.task_queue)
        
        # 오토스케일링
        if q_len >= 6 and self.get_current_spot_scale() < MAX_SPOT_SCALE - 1:
            if self.scale_out_worker("spot_a"): pass
            if self.scale_out_worker("spot_a"): pass
        elif q_len >= 2 and self.get_current_spot_scale() < MAX_SPOT_SCALE:
            self.scale_out_worker("spot_a")
            
        if q_len == 0:
            self.scale_in_timer += self.tick_rate
            if self.scale_in_timer >= _SP["scale_in_sec_sim"]:
                if self.scale_in_worker("spot_a"):
                    self.scale_in_timer = 0.0
        else:
            self.scale_in_timer = 0.0

        # 배정 루프
        while self.task_queue:
            # 가용 IDLE 노드 탐색 (On-Demand 우선)
            idle_workers = [w for w in self.workers.values() if w.status == "IDLE" and w.node_type in ["on_demand", "spot_a"]]
            if not idle_workers:
                break
                
            idle_workers.sort(key=lambda x: 0 if x.node_type == "on_demand" else 1)
            target_worker = idle_workers[0]
            
            task = self.task_queue.pop(0)
            self.assign_task_to_worker(task, target_worker)

    def run_dynamic_scheduler(self):
        # Dynamic: Safety-First + Spot-B 우선 + Backfilling
        q_len = len(self.task_queue)
        
        # 스마트 스케일아웃 결정: 큐 내의 LSTM 개수와 현재 가동 중인 spot_a 대수를 비교하여 필요한 경우에만 Spot-A 증설
        num_lstm = sum(1 for t in self.task_queue if t["model_type"] == "LSTM")
        active_spot_a = sum(1 for w in self.workers.values() if w.node_type == "spot_a")
        target_type = "spot_a" if (num_lstm > active_spot_a) else "spot_b"
        
        # 부하 인지 오토스케일링
        if q_len >= 8 and self.get_current_spot_scale() < MAX_SPOT_SCALE - 1:
            self.scale_out_worker(target_type)
            self.scale_out_worker(target_type)
        elif q_len >= 2 and self.get_current_spot_scale() < MAX_SPOT_SCALE:
            self.scale_out_worker(target_type)
            
        if q_len == 0:
            self.scale_in_timer += self.tick_rate
            if self.scale_in_timer >= _SP["scale_in_sec_dynamic"]:  # 임계치 완화 적용 (Cold Start 플래핑 제거)
                has_spot_a = any(w.node_type == "spot_a" for w in self.workers.values())
                reclaim_type = "spot_a" if has_spot_a else "spot_b"
                if self.scale_in_worker(reclaim_type):
                    self.scale_in_timer = 0.0
        else:
            self.scale_in_timer = 0.0

        # 배정 루프 (Backfilling 지원)
        deferred = []
        while self.task_queue:
            task = self.task_queue.pop(0)
            model = task["model_type"]
            
            # 선호 리스트 및 하드 금지 (유일 진실: sim_env.yaml scheduler_policy)
            pref = _SP["model_node_preference"].get(model, ["on_demand", "spot_b", "spot_a"])
            forbidden = set(_SP["model_forbidden"].get(model, []))
            
            # 가용 노드 스캔
            idle_workers = [w for w in self.workers.values() if w.status == "IDLE" and w.node_type not in forbidden]
            if not idle_workers:
                deferred.append(task)
                continue
                
            # 선호 순서 정렬
            idle_workers.sort(key=lambda w: pref.index(w.node_type) if w.node_type in pref else 99)
            target_worker = idle_workers[0]
            
            self.assign_task_to_worker(task, target_worker)
            
        # 대기열 환원
        if deferred:
            self.task_queue = deferred + self.task_queue

    def get_current_state_key(self):
        q_len = len(self.task_queue)
        head_model = self.task_queue[0]["model_type"] if self.task_queue else None
        head_time_left = max(0.0, self.task_queue[0]["deadline"] - self.current_time) if self.task_queue else None
        
        idle_od = any(w.node_type == "on_demand" and w.status == "IDLE" for w in self.workers.values())
        idle_spot_a = any(w.node_type == "spot_a" and w.status == "IDLE" for w in self.workers.values())
        idle_spot_b = any(w.node_type == "spot_b" and w.status == "IDLE" for w in self.workers.values())
        
        danger_phase = FailureSimulator.current_danger_phase(self.current_time)
        
        return state_features.compute_state(
            q_len, head_model, head_time_left,
            idle_od, idle_spot_a, idle_spot_b,
            self.virtual_budget, danger_phase
        )

    def run_q_learning_scheduler(self):
        if not self.q_agent:
            return
            
        if not self.q_learning_training:
            self.q_agent.epsilon = 0.0  # 평가 모드에서는 탐험율 0 고정
            
        while self.task_queue:
            # 1. 상태 변수 추출 및 인코딩
            state = self.get_current_state_key()
            
            # 2. 가용 행동 필터링 (Action Masking)
            available_actions = [3]  # HOLD는 기본 가용
            
            idle_od = any(w.node_type == "on_demand" and w.status == "IDLE" for w in self.workers.values())
            idle_spot_a = any(w.node_type == "spot_a" and w.status == "IDLE" for w in self.workers.values())
            idle_spot_b = any(w.node_type == "spot_b" and w.status == "IDLE" for w in self.workers.values())
            
            # 백필링(Backfilling) 지원: 각 노드 타입별로 현재 대기열에서 가용한 첫 번째 적합 태스크 스캔
            runnable_for_od = None
            runnable_for_spot_a = None
            runnable_for_spot_b = None
            
            for t in self.task_queue:
                model = t["model_type"]
                if runnable_for_od is None:
                    runnable_for_od = t
                if runnable_for_spot_a is None:
                    runnable_for_spot_a = t
                if runnable_for_spot_b is None:
                    if model != "LSTM":  # LSTM은 Spot-B 금지
                        runnable_for_spot_b = t
                        
            if idle_od and runnable_for_od:
                available_actions.append(0)
            if idle_spot_a and runnable_for_spot_a:
                available_actions.append(1)
            if idle_spot_b and runnable_for_spot_b:
                available_actions.append(2)
                
            spot_scale = self.get_current_spot_scale()
            has_launching = any(w.status == "BOOTING" for w in self.workers.values())
            
            if spot_scale < MAX_SPOT_SCALE and not has_launching:
                available_actions.append(4)  # SCALE_OUT_SPOT_A
                available_actions.append(5)  # SCALE_OUT_SPOT_B
                
            # 예산 고갈 시 마스킹
            if self.virtual_budget <= 0.0:
                if 0 in available_actions: available_actions.remove(0)
                if 4 in available_actions: available_actions.remove(4)
                if 5 in available_actions: available_actions.remove(5)
                
            # Starvation 가드
            head_time_left = max(0.0, self.task_queue[0]["deadline"] - self.current_time)
            is_urgent = head_time_left <= state_features.SLA_TIGHT_SEC
            if is_urgent and any(act in available_actions for act in [0, 1, 2]):
                if 3 in available_actions:
                    available_actions.remove(3)
                    
            if available_actions == [3] and not any(act in available_actions for act in [0, 1, 2, 4, 5]):
                # 마스킹 등으로 HOLD밖에 선택지가 없으면 루프 탈출
                break
                
            action = self.q_agent.choose_action(state, available_actions)
            
            if action in [0, 1, 2]:
                target_type = ["on_demand", "spot_a", "spot_b"][action]
                target_task = None
                target_idx = -1
                
                # 선택된 노드타입에 부합하는 첫 번째 태스크 찾아서 팝(Pop)
                for idx, t in enumerate(self.task_queue):
                    if target_type == "spot_b" and t["model_type"] == "LSTM":
                        continue
                    target_task = t
                    target_idx = idx
                    break
                    
                if target_task:
                    self.task_queue.pop(target_idx)
                    worker_id = None
                    for wid, w in self.workers.items():
                        if w.node_type == target_type and w.status == "IDLE":
                            worker_id = wid
                            break
                            
                    if worker_id:
                        self.assign_task_to_worker(target_task, self.workers[worker_id])
                        # 완료/실패 시 지연보상 피드백을 위해 바인딩
                        self.running_tasks[target_task["task_id"]] = {
                            "state": state,
                            "action": action
                        }
                    else:
                        # 예기치 않게 워커 매칭에 실패한 경우 대기열 원래 인덱스로 롤백
                        self.task_queue.insert(target_idx, target_task)
                        break
                else:
                    break  # 적합한 태스크가 대기열에 없음
                    
                    
            elif action == 3:  # HOLD
                if self.q_learning_training:
                    overdue = [max(0.0, self.current_time - t["deadline"]) for t in self.task_queue]
                    reward = reward_policy.hold_reward(len(self.task_queue), overdue, self.q_agent.DELAY_PENALTY_WEIGHT)
                    next_state = self.get_current_state_key()
                    self.q_agent.update_q_value(state, action, reward, next_state)
                    self.q_agent.save_q_table()
                break  # HOLD 시 이번 틱 대기
                
            elif action in [4, 5]:  # SCALE_OUT
                target_type = "spot_a" if action == 4 else "spot_b"
                scale_success = self.scale_out_worker(target_type)
                
                if self.q_learning_training:
                    cost_level = 1 if (BUDGET_LIMIT - self.virtual_budget) > state_features.COST_LEVEL_THRESHOLD else 0
                    reward = reward_policy.scale_reward(action, urgent=is_urgent, cost_level=cost_level, scale_success=scale_success)
                    next_state = self.get_current_state_key()
                    self.q_agent.update_q_value(state, action, reward, next_state)
                    self.q_agent.save_q_table()
                break  # 증설 가동 대기 위해 이번 틱 루프 탈출

    def assign_task_to_worker(self, task, worker):
        worker.status = "BUSY"
        worker.active_task = task
        task["submit_time"] = self.current_time
        
        # 오프라인 학습기(pretrain.py)의 실측 기반 캘리브레이션 공식과 동기화
        model = task["model_type"]
        gpu_s = NODES_CONFIG[worker.node_type]["gpu_scale"]
        
        if model == "MERGE":
            exec_duration = _ET["merge_sec"]
        else:
            per_epoch = _ET["per_epoch"].get(model, 0.04)
            exec_duration = _ET["floor"] + (task["epochs"] * per_epoch) / gpu_s
            
        worker.task_end_time = self.current_time + exec_duration

    def tick(self):
        # 1. 부팅 중인 워커 상태 갱신
        for w in self.workers.values():
            if w.status == "BOOTING":
                w.boot_time_left -= self.tick_rate
                if w.boot_time_left <= 0:
                    w.status = "IDLE"

        # 2. 실행 중인 태스크들 갱신
        busy_workers = [w for w in self.workers.values() if w.status == "BUSY"]
        for w in busy_workers:
            # 틱 단위 OOM 체크 (현실화된 확률 대입)
            if FailureSimulator.check_oom(w.active_task["model_type"], w.active_task["task_id"], w.node_type, co_membound_count=0):
                # OOM-Kill 발생! 물리 노드 즉각 사망 (Static 대량 페널티)
                self.handle_node_death(w.worker_id, was_evicted=False, was_oom=True)
                continue
                
            if self.current_time >= w.task_end_time:
                # 성공적 완료
                task = w.active_task
                exec_time = self.current_time - task["submit_time"]
                cph = NODES_CONFIG[w.node_type]["cost_per_hour"]
                task_cost = cph * (exec_time / 3600.0)
                delay = max(0.0, self.current_time - task["deadline"])
                
                self.logs.append([
                    self.get_simulated_timestamp(),
                    self.mode,
                    task["task_id"],
                    task["model_type"],
                    "SUCCESS",
                    round(exec_time, 4),
                    round(task_cost, 6),
                    round(delay, 4),
                    sum(1 for wk in self.workers.values() if wk.node_type == "on_demand"),
                    sum(1 for wk in self.workers.values() if wk.node_type == "spot_a"),
                    sum(1 for wk in self.workers.values() if wk.node_type == "spot_b"),
                    round(self.virtual_budget, 6)
                ])
                
                # --- Q-Learning 피드백 업데이트 ---
                if self.mode == "q_learning" and self.q_agent and task["task_id"] in self.running_tasks:
                    binding = self.running_tasks[task["task_id"]]
                    state = binding["state"]
                    action = binding["action"]
                    
                    reward = self.q_agent.calculate_reward(
                        success=True,
                        execution_time=exec_time,
                        worker_type=w.node_type,
                        delay_time=delay,
                        deadline_exceeded=self.current_time > task["deadline"],
                        current_model=task["model_type"],
                        co_scheduled_models=[],
                        evicted=False,
                        oom=False
                    )
                    
                    if self.q_learning_training:
                        next_state = self.get_current_state_key()
                        self.q_agent.update_q_value(state, action, reward, next_state)
                        self.q_agent.save_q_table()
                    
                    del self.running_tasks[task["task_id"]]

                # 워커 상태 복구
                w.status = "IDLE"
                w.active_task = None

        # 3. 실시간 요금 차감
        cost_per_hour = sum(NODES_CONFIG[w.node_type]["cost_per_hour"] for w in self.workers.values() if w.status != "BOOTING")
        self.virtual_budget -= (cost_per_hour / 3600.0) * self.tick_rate

        # 4. Eviction 데몬 주기 구동 (10초 주기)
        self.eviction_timer += self.tick_rate
        if self.eviction_timer >= EVICTION_POLL_SEC:
            self.check_eviction_daemon()
            self.eviction_timer = 0.0

        # 시간 증분
        self.current_time += self.tick_rate

    def generate_mock_tasks(self):
        # 0.6초(3 tick) 주기로 신규 태스크 생성 모사 (tick_rate=0.2s)
        tick_counter = int(round(self.current_time / self.tick_rate))
        if tick_counter % 3 != 0:
            return
            
        tasks_cap = _WL["tasks_cap"]
        if self.total_generated_tasks >= tasks_cap:
            return

        model_types = _WL["model_types"]
        is_burst = random.random() < self.burst_prob
        is_normal = not is_burst and (random.random() < self.normal_prob)

        num_new_tasks = 0
        if is_burst:
            num_new_tasks = min(random.randint(_WL["burst_min"], _WL["burst_max"]), tasks_cap - self.total_generated_tasks)
        elif is_normal:
            num_new_tasks = 1

        for _ in range(num_new_tasks):
            self.total_generated_tasks += 1
            task_id = f"task-{self.total_generated_tasks:04d}"
            model = random.choice(model_types)
            epochs = random.randint(_WL["epochs"]["min"], _WL["epochs"]["max"])
            timeout = random.randint(_WL["timeout"]["min"], _WL["timeout"]["max"])
            deadline = self.current_time + timeout
            self.task_queue.append({
                "task_id": task_id,
                "model_type": model,
                "epochs": epochs,
                "deadline": deadline,
                "attempts": 0,
                "submit_time": 0.0
            })

    def run_to_end(self, write_csv=True):
        """예산이 바닥나거나 모든 태스크 처리가 끝나면 종료. write_csv=True면 대표 CSV를 남긴다."""
        while self.virtual_budget > 0.0:
            self.generate_mock_tasks()

            if (self.total_generated_tasks >= _WL["tasks_cap"] and not self.task_queue and not any(w.status == "BUSY" for w in self.workers.values())) or self.current_time >= 500.0:
                break

            # 스케줄러 주기 작동 (0.2초 tick과 싱크)
            if self.mode == "static":
                self.run_static_scheduler()
            elif self.mode == "dynamic":
                self.run_dynamic_scheduler()
            elif self.mode == "q_learning":
                self.run_q_learning_scheduler()

            self.tick()

        # CSV 파일에 수집된 기록 저장 (visualize_benchmarks.py 호환용 대표 1회분)
        if write_csv and not (self.mode == "q_learning" and self.q_learning_training):
            self.write_logs_csv()
        return self.logs

    def write_logs_csv(self):
        """수집 로그를 data/benchmark_results_<mode>.csv 로 저장(헤더 없음, 기존 12컬럼 스키마)."""
        csv_path = os.path.join(DATA_DIR, f"benchmark_results_{self.mode}.csv")
        os.makedirs(DATA_DIR, exist_ok=True)
        import csv
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            for log in self.logs:
                writer.writerow(log)

