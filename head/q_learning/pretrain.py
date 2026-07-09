# ==============================================================================
# WE-MEET: Q-Learning 오프라인 사전 학습기 (head/q_learning/pretrain.py)
#
# [목적] docker 실전 투입 전, Q-테이블을 미리 데워두는 warm-start 생성기.
#   산출물 data/q_table.json 을 docker 부팅 시 agent.load_q_table()가 읽어 곧바로 추론에 사용하고,
#   Q_LEARNING_TRAINING_MODE=True면 이 warm-start에서 이어받아 실전 온라인 미세조정을 한다.
#
# [세계 통일 원칙] 이 시뮬레이터는 실제 docker 경로(head/)의 물리를 '미러링'한다:
#   - 노드 3종(on_demand/spot_a/spot_b)·성능계수(gpu_scale)·요금(cost_per_hour)
#   - 노출시간 기반 스팟 회수: 10초 폴링 + 30초 위험구간 사이클
#   - 자원경합 기반 OOM(FailureSimulator.check_oom)
#   - 6대 행동(0~5)과 state_features.compute_state()의 동일한 6-튜플 상태
#   - ASSIGN 액션의 지연 보상(태스크가 실제 완료/회수될 때 그 배정 (state,action)에 보상 귀속)
#   덕분에 여기서 학습한 Q-테이블이 추론(docker)에서 그대로 통한다(sim-to-real).
#
# 50,000 에피소드를 실제 docker로 학습하면 에피소드당 수 분이 걸려 며칠~몇 주가 소요되므로,
# 대량 사전학습은 이 빠른 미러링 시뮬레이터에서 수행한다.
# ==============================================================================

import os
import sys
import random

# 프로젝트 루트 디렉토리를 path에 추가하여 head/common 패키지 임포트 지원
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from head.q_learning.agent import QLearningAgent
import head.q_learning.state_features as state_features
from common.failure_simulator import FailureSimulator

DT = 1.0                                   # 1틱 = 1 시뮬레이션 초 (실제 회수 폴링 10초와 동일 축척)
EVICTION_POLL_SEC = 10.0                   # 실제 eviction_loop 폴링 주기와 동일
EPOCH_TIMES = {"CNN": 0.22, "RNN": 0.10, "LSTM": 0.10}
MAX_SPOT_SCALE = 6
MODEL_TYPES = ["CNN", "RNN", "LSTM"]


class SimulatedEnvironment:
    """실제 docker 스케줄링 세계를 미러링하는 학습용 시뮬레이션 환경."""

    def __init__(self, cost_model_path=None):
        self.agent = QLearningAgent(cost_model_path=cost_model_path)
        self.cfg = self.agent.nodes_config
        self.reset()

    # ---------------------------------------------------------------- 환경 초기화
    def reset(self):
        self.sim_time = 0.0
        self.next_evict_time = EVICTION_POLL_SEC
        # 예산 축의 모든 국면(위험/낮음/여유)과 '실제 고갈'을 학습에서 겪도록 초기 예산을 낮게 무작위화.
        # (실제 벤치마크 시나리오 예산 $1.5 부근을 중심으로 분포시켜 예산 압박을 실제로 체감하게 한다)
        self.virtual_budget = random.uniform(0.5, 3.0)
        self.task_counter = 0
        self.task_queue = []

        # 워커: worker-1(OD) 상시 1대, spot_a/spot_b는 0대에서 동적 증설
        self.workers = {
            "worker-1": {"type": "on_demand", "status": "IDLE", "task": None,
                         "remaining": 0.0, "exec_time": 0.0, "s": None, "a": None},
        }
        self.spot_seq = 0

        for _ in range(random.randint(5, 8)):
            self._generate_task()
        return self._get_state()

    def _generate_task(self):
        self.task_counter += 1
        model = random.choice(MODEL_TYPES)
        epochs = random.randint(12, 20)
        timeout = random.randint(5, 12)   # 실제 scheduler_daemon과 동일하게 5~12초로 단축하여 타이트하게 학습
        self.task_queue.append({
            "task_id": f"sim-task-{self.task_counter:04d}",
            "model_type": model,
            "epochs": epochs,
            "deadline": self.sim_time + timeout,
        })

    def _compute_exec_time(self, task, node_type):
        gpu = self.cfg.get(node_type, {}).get("gpu_scale_factor", 1.0)
        return (task["epochs"] * EPOCH_TIMES.get(task["model_type"].upper(), 0.10)) / max(0.1, gpu)

    # ---------------------------------------------------------------- 상태 산출
    def _danger_phase(self):
        return 1 if (self.sim_time % 30.0) < 10.0 else 0

    def _idle(self, ntype):
        return any(w["status"] == "IDLE" and w["type"] == ntype for w in self.workers.values())

    def _spot_count(self):
        return sum(1 for w in self.workers.values() if w["type"] in ("spot_a", "spot_b"))

    def _get_state(self):
        head = self.task_queue[0] if self.task_queue else None
        head_model = head["model_type"] if head else None
        head_time_left = (head["deadline"] - self.sim_time) if head else None
        return state_features.compute_state(
            q_len=len(self.task_queue),
            head_model=head_model,
            head_time_left=head_time_left,
            idle_od=self._idle("on_demand"),
            idle_spot_a=self._idle("spot_a"),
            idle_spot_b=self._idle("spot_b"),
            budget=self.virtual_budget,
            danger_phase=self._danger_phase(),
        )

    def available_actions(self):
        acts = [3]  # HOLD 항상 가능
        head = self.task_queue[0] if self.task_queue else None
        if head is not None:
            if self._idle("on_demand"):
                acts.append(0)
            if self._idle("spot_a"):
                acts.append(1)
            if self._idle("spot_b"):
                acts.append(2)
        if self._spot_count() < MAX_SPOT_SCALE:
            acts.append(4)
            acts.append(5)
        # 예산 고갈 시 고비용 행동(OD 배정/증설) 마스킹
        if self.virtual_budget <= 0.0:
            for bad in (0, 4, 5):
                if bad in acts:
                    acts.remove(bad)
        # 마감 임박(<=10s) 시 HOLD 억제 (실제 q_learning.py 마스킹과 동일) → 무행동 함정 방지
        if head is not None and (head["deadline"] - self.sim_time) <= 10.0:
            if any(a in acts for a in (0, 1, 2)) and 3 in acts:
                acts.remove(3)
        return acts if acts else [3]

    # ---------------------------------------------------------------- 보상
    def _terminal_reward(self, task, node_type, exec_elapsed, success, evicted):
        delay = max(0.0, self.sim_time - task["deadline"])
        deadline_exceeded = self.sim_time > task["deadline"]
        return self.agent.calculate_reward(
            success=success,
            execution_time=exec_elapsed,
            worker_type=node_type,
            delay_time=delay,
            deadline_exceeded=deadline_exceeded,
            current_model=task["model_type"],
            co_scheduled_models=[],
            evicted=evicted,
        )

    def _finalize(self, worker, success, evicted):
        """실행 중이던 태스크를 종료 처리하고, 배정 시점 (s,a)에 지연 보상을 귀속시켜 Q-업데이트."""
        task = worker["task"]
        node_type = worker["type"]
        elapsed = worker["exec_time"] - max(0.0, worker["remaining"]) if not success else worker["exec_time"]
        reward = self._terminal_reward(task, node_type, max(0.0, elapsed), success, evicted)
        s, a = worker["s"], worker["a"]
        if s is not None and a is not None:
            self.agent.update_q_value(s, a, reward, self._get_state())
        # 워커 상태 정리
        worker["task"] = None
        worker["status"] = "IDLE"
        worker["remaining"] = 0.0
        worker["s"] = None
        worker["a"] = None
        if not success:
            self.task_queue.insert(0, task)  # 실패분 재큐잉 (Lineage 복구 모사)

    # ---------------------------------------------------------------- 1스텝 전이
    def step(self, state, action):
        self.sim_time += DT

        # 1) 실시간 예산 차감 (활성 노드 요금)
        for w in self.workers.values():
            cph = self.cfg.get(w["type"], {}).get("cost_per_hour", 0.0)
            self.virtual_budget -= cph * (DT / 3600.0)

        # 2) 진행 중 태스크 시간 경과 및 완료 처리 (지연 보상 귀속)
        for w in list(self.workers.values()):
            if w["status"] == "BUSY" and w["task"] is not None:
                w["remaining"] -= DT
                if w["remaining"] <= 0.0:
                    self._finalize(w, success=True, evicted=False)

        # 3) 노출시간 기반 스팟 회수 폴링 (실제 eviction_loop 미러링)
        if self.sim_time >= self.next_evict_time:
            self.next_evict_time += EVICTION_POLL_SEC
            p_spot = self._danger_phase()
            for wid, w in list(self.workers.items()):
                if w["type"] in ("spot_a", "spot_b"):
                    if FailureSimulator.check_eviction(w["type"], p_spot):
                        # 실행 중이던 태스크는 회수 실패로 종료(강한 페널티) 후 워커 제거(스케일 다운)
                        if w["status"] == "BUSY" and w["task"] is not None:
                            self._finalize(w, success=False, evicted=True)
                        del self.workers[wid]

        # 4) 신규 태스크 유입 (실제 docker의 0.6초 주기당 4% burst / 18% normal을 1초 주기로 보정 매핑)
        is_burst = random.random() < 0.067
        is_normal = not is_burst and (random.random() < 0.30)
        if is_burst:
            num_new = random.randint(5, 8)
            for _ in range(num_new):
                self._generate_task()
        elif is_normal:
            self._generate_task()

        # 5) 에이전트 행동 적용
        if action in (0, 1, 2):
            ntype = ["on_demand", "spot_a", "spot_b"][action]
            self._try_assign(ntype, state, action)
        elif action == 3:
            # HOLD: 즉시 보상(대기 적체·마감 초과 페널티) → 즉시 Q-업데이트. 실제 q_learning.py와 동일 수식.
            hold_penalty = 0.0
            for t in self.task_queue:
                over = self.sim_time - t["deadline"]
                if over > 0.0:
                    hold_penalty += over * self.agent.DELAY_PENALTY_WEIGHT * 0.2
            reward = 1.0 - 0.5 * len(self.task_queue) - hold_penalty
            self.agent.update_q_value(state, action, reward, self._get_state())
        elif action in (4, 5):
            ntype = "spot_a" if action == 4 else "spot_b"
            reward = self._scale_out(ntype)
            self.agent.update_q_value(state, action, reward, self._get_state())

        done = self.virtual_budget <= 0.0
        return self._get_state(), done

    def _try_assign(self, ntype, state, action):
        # 해당 타입 IDLE 워커 탐색
        target = None
        for w in self.workers.values():
            if w["type"] == ntype and w["status"] == "IDLE":
                target = w
                break
        if target is None or not self.task_queue:
            # 불가능한 배정 시도 → 경미한 즉시 페널티
            self.agent.update_q_value(state, action, -1.0, self._get_state())
            return
        task = self.task_queue.pop(0)
        # 자원경합 OOM 사전 판정(동거는 1노드 1태스크라 0). 실패 시 즉시 종료 후 재큐잉.
        if FailureSimulator.check_oom(task["model_type"], task["task_id"], ntype, 0):
            reward = self._terminal_reward(task, ntype, 0.0, success=False, evicted=False)
            self.agent.update_q_value(state, action, reward, self._get_state())
            self.task_queue.insert(0, task)
            return
        exec_time = self._compute_exec_time(task, ntype)
        target["status"] = "BUSY"
        target["task"] = task
        target["exec_time"] = exec_time
        target["remaining"] = exec_time
        target["s"] = state       # 지연 보상 귀속용 (배정 시점 상태/행동 기록)
        target["a"] = action

    def _scale_out(self, ntype):
        if self._spot_count() >= MAX_SPOT_SCALE:
            return -1.5  # 한도 초과
        # OutOfCapacity 모사
        if FailureSimulator.check_out_of_capacity(ntype):
            return -10.0
        self.spot_seq += 1
        wid = f"{'worker-2' if ntype == 'spot_a' else 'worker-3'}-{self.spot_seq}"
        self.workers[wid] = {"type": ntype, "status": "IDLE", "task": None,
                             "remaining": 0.0, "exec_time": 0.0, "s": None, "a": None}
        # 증설 자체는 요금 부담(감점), 저가 노드일수록 부담이 작다
        return -1.0 if ntype == "spot_a" else -0.5


def train_offline(episodes=50000, cost_model_path=None):
    """시뮬레이션 환경(실제 세계 미러링)을 통해 Q-Learning 에이전트를 오프라인 사전 훈련한다."""
    print("=== [Q-Learning] 오프라인 시뮬레이션 사전 훈련 시작 (세계 미러링) ===")
    env = SimulatedEnvironment(cost_model_path=cost_model_path)
    agent = env.agent

    agent.epsilon = 1.0
    agent.epsilon_min = 0.05
    agent.decay_rate = 0.99997  # 상태공간이 넓어 부드러운 감쇄

    solvent_episodes = 0

    for ep in range(1, episodes + 1):
        state = env.reset()
        for _ in range(400):  # 에피소드당 최대 400틱 (예산 고갈 동역학을 겪기에 충분한 길이)
            actions = env.available_actions()
            action = agent.choose_action(state, actions)
            state, done = env.step(state, action)
            if done:
                break

        if env.virtual_budget > 0.0:
            solvent_episodes += 1

        if ep % 2000 == 0:
            print(f"Episode {ep:6d}/{episodes} | 상태수: {len(agent.q_table):5d} | "
                  f"예산생존율: {(solvent_episodes/2000)*100:5.1f}% | Epsilon: {agent.epsilon:.4f}")
            solvent_episodes = 0

    agent.save_q_table()
    print(f"=== [Q-Learning] 사전 훈련 완료. 학습 상태수={len(agent.q_table)} | 저장경로={agent.q_table_path} ===")


def run_offline_pretraining(episodes=50000):
    """실제 docker 물리를 미러링한 시뮬레이터로 Q-Learning 에이전트를 사전 훈련한다(warm-start 생성)."""
    cost_model_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../common/cost_model.yaml'))
    train_offline(episodes=episodes, cost_model_path=cost_model_path)


if __name__ == "__main__":
    run_offline_pretraining()
