"""WE-MEET: Q-Learning 오프라인 사전 학습기 (head/q_learning/pretrain.py)

[목적] docker 실전 투입 전, Q-테이블을 미리 데워두는 warm-start 생성기.
  산출물 data/q_table.json 을 docker 부팅 시 agent.load_q_table()가 읽어 곧바로 추론에 사용하고,
  Q_LEARNING_TRAINING_MODE=True면 이 warm-start에서 이어받아 실전 온라인 미세조정을 한다.

[세계 통일 원칙] 이 시뮬레이터는 실제 docker 경로(head/scheduler/)의 물리·행동·보상·종료
  동역학을 최대한 '미러링'한다. 학습 결과가 추론에서 그대로 통하도록(sim-to-real):
  - 상태 인코딩: state_features.compute_state (실제와 동일한 6-튜플)
  - ASSIGN(0/1/2) 지연 보상: agent.calculate_reward (실제와 동일한 완료-시점 귀속)
  - HOLD(3)/SCALE(4,5) 보상: reward_policy (실제 q_learning.py와 공유하는 유일 진실)
  - 물리: cost_model.yaml 요금·gpu_scale, FailureSimulator의 회수/OOM/OutOfCapacity 확률
  - 회수: 10초 폴링 + 30초 위험구간(앞 10초) 사이클
  - 예산: 활성 노드 요금 실시간 차감, 0 이하면 파산(에피소드 종료)
  - 스케줄러 드레인: 한 틱에 유휴 워커가 있는 한 연속 배정(실제 while-loop), HOLD/SCALE는 틱 종료
  - scale-in: 유휴 spot 3초 지속 시 1대 자동 축소
  - 재시도 캡: 태스크가 3회 실패하면 재큐 대신 영구 유실(DEAD_LETTER)

[의도적 단순화 — 실제와 다르지만 근거 있음]
  - Map-Reduce 미모델: 실제는 epochs>=8 태스크를 최대 3워커로 분할(mock은 거의 전부 해당)하지만,
    충실 재현은 구현 복잡도가 크고 잘못 모델하면 오히려 새 divergence를 낳는다. 대신 '1태스크=1워커'로
    두되 실행시간을 실측 벤치 CSV 분포(성공 ~1.5s floor)에 맞춰 보정한다. 이에 따라 동거(co-scheduling)
    압력도 발생하지 않으므로 co_membound=0으로 둔다(실제도 1태스크-1워커 정상경로에선 대개 0).
  - MAX_SPOT_SCALE: 실제는 호스트 RAM 기반 max(5, 추천)이라 오프라인서 재현 불가 → 대표값 고정.
  - 초기 예산 uniform(0.5,3.0): 실제 기본 예산 $1.5는 budget_level 0/1만 점유한다. 이 분포는 그 국면을
    충실히 덮으므로 의도된 정렬이다(level 2를 억지로 덮으려 범위를 넓히지 말 것 — 실제엔 없는 상태).

50,000 에피소드를 실제 docker로 학습하면 에피소드당 수 분이 걸려 며칠~몇 주가 소요되므로,
대량 사전학습은 이 빠른 미러링 시뮬레이터에서 수행한다.
"""

import os
import sys
import random

# 프로젝트 루트 디렉토리를 path에 추가하여 wemeet 패키지 임포트 지원
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from wemeet.learning.agent import QLearningAgent
import wemeet.learning.state_features as state_features
import wemeet.learning.reward_policy as reward_policy
from wemeet.simulation.failure_simulator import FailureSimulator
from wemeet.config import env_config as _ec

# 아래 수치의 유일 진실은 sim_env.yaml + cost_model.yaml (env_config). 여기서는 그 값을 로드해 쓴다.
_WL = _ec.workload()
_ET = _ec.exec_time()

DT = 1.0                                   # 1틱 = 1 시뮬레이션 초 (실제 회수 폴링 10초와 동일 축척)
EVICTION_POLL_SEC = _ec.failure()["eviction"]["poll_sec"]   # 실제 eviction_loop 폴링 주기와 동일
MAX_SPOT_SCALE = _ec.scaling()["max_spot_scale_floor"]      # 실제 scheduler_daemon의 max(5, 추천) 대표 고정값
MODEL_TYPES = _WL["model_types"]

# 재시도 캡: head/scheduler/task_executor.py::MAX_TASK_ATTEMPTS 와 공유(sim_env.yaml scaling).
MAX_TASK_ATTEMPTS = _ec.scaling()["max_task_attempts"]

# --- 실행시간 캘리브레이션 (실측 data/benchmark_results_q_learning.csv 성공 행 기준) ---
# 실제 성공 실행시간은 폴링 granularity로 ~1.5s floor에 몰리고(p50 1.53), 소폭의 모델·노드 의존 꼬리를
# 갖는다(p90 ~4-6s). epochs 선형 모델은 형태가 틀리므로 'floor + 작은 가변항 / gpu_scale'로 맞춘다.
EXEC_FLOOR = _ET["floor"]
PER_EPOCH = _ET["per_epoch"]   # 연산바운드 CNN이 가장 무겁게


class SimulatedEnvironment:
    """실제 docker 스케줄링 세계를 미러링하는 학습용 시뮬레이션 환경."""

    # 상태 파생에 쓰는 시간당 요금 — cost_model.yaml(env_config) 유일 진실. state_features 와 동일.
    COST_PER_HOUR = _ec.cost_per_hour()

    def __init__(self, cost_model_path=None):
        self.agent = QLearningAgent(cost_model_path=cost_model_path)
        self.cfg = self.agent.nodes_config
        self.reset()

    # ---------------------------------------------------------------- 환경 초기화
    def reset(self):
        self.sim_time = 0.0
        self.next_evict_time = EVICTION_POLL_SEC
        # 예산 축의 모든 국면(위험/낮음)과 '실제 고갈'을 학습에서 겪도록 초기 예산을 낮게 무작위화.
        # (실제 벤치마크 시나리오 예산 $1.5 부근 → budget_level 0/1을 충실히 덮는다)
        _lo, _hi = _ec.budget()["sim_range"]
        self.virtual_budget = random.uniform(_lo, _hi)
        self.task_counter = 0
        self.task_queue = []
        self.idle_spot_duration = 0.0   # 유휴 spot 지속 시간(scale-in 타이머)

        # 워커: worker-1(OD) 상시 1대, spot_a/spot_b는 0대에서 동적 증설
        self.workers = {
            "worker-1": {"type": "on_demand", "status": "IDLE", "task": None,
                         "remaining": 0.0, "exec_time": 0.0, "s": None, "a": None},
        }
        self.spot_seq = 0

        for _ in range(random.randint(_WL["burst_min"], _WL["burst_max"])):
            self._generate_task()
        return self._get_state()

    def _generate_task(self):
        self.task_counter += 1
        model = random.choice(MODEL_TYPES)
        epochs = random.randint(_WL["epochs"]["min"], _WL["epochs"]["max"])
        timeout = random.randint(_WL["timeout"]["min"], _WL["timeout"]["max"])   # 실제 scheduler_daemon과 동일
        self.task_queue.append({
            "task_id": f"sim-task-{self.task_counter:04d}",
            "model_type": model,
            "epochs": epochs,
            "deadline": self.sim_time + timeout,
            "attempts": 0,
        })

    def _compute_exec_time(self, task, node_type):
        gpu = self.cfg.get(node_type, {}).get("gpu_scale_factor", 1.0)
        per = PER_EPOCH.get(task["model_type"].upper(), 0.04)
        return EXEC_FLOOR + (task["epochs"] * per) / max(0.1, gpu)

    # ---------------------------------------------------------------- 상태/관측
    def _danger_phase(self):
        return FailureSimulator.current_danger_phase(self.sim_time)

    def _idle(self, ntype):
        return any(w["status"] == "IDLE" and w["type"] == ntype for w in self.workers.values())

    def _spot_count(self):
        return sum(1 for w in self.workers.values() if w["type"] in ("spot_a", "spot_b"))

    def _has_idle_spot(self):
        return any(w["status"] == "IDLE" and w["type"] in ("spot_a", "spot_b") for w in self.workers.values())

    def _cost_level(self):
        """실제 state_features와 동일: 총 시간당요금 > $9면 고비용 국면(1). OD+spot_a 1대면 이미 9.30."""
        total = sum(self.COST_PER_HOUR.get(w["type"], 0.0) for w in self.workers.values())
        return 1 if total > state_features.COST_LEVEL_THRESHOLD else 0

    def _head(self):
        return self.task_queue[0] if self.task_queue else None

    def _urgent(self):
        head = self._head()
        return head is not None and (head["deadline"] - self.sim_time) <= state_features.SLA_TIGHT_SEC

    def _get_state(self):
        head = self._head()
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
        """실제 q_learning.py의 액션 마스킹 규칙을 미러링."""
        acts = [3]  # HOLD 항상 가능
        head = self._head()
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
        # 마감 임박 시 HOLD 억제(무행동 함정 방지) — 실제와 동일
        if self._urgent() and any(a in acts for a in (0, 1, 2)) and 3 in acts:
            acts.remove(3)
        return acts if acts else [3]

    # ---------------------------------------------------------------- 보상 (ASSIGN 완료-시점)
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
            co_scheduled_models=[],   # Map-Reduce 미모델 → 동거 없음(파일 상단 근거 주석 참조)
            evicted=evicted,
        )

    def _requeue_or_deadletter(self, task):
        """실패 태스크 재큐 — 단, 3회 도달 시 재큐하지 않고 영구 유실(실제 DEAD_LETTER 미러)."""
        task["attempts"] = task.get("attempts", 0) + 1
        if task["attempts"] >= MAX_TASK_ATTEMPTS:
            return  # 드롭(큐 무한 점유 차단)
        self.task_queue.insert(0, task)

    def _finalize(self, worker, success, evicted):
        """실행 중이던 태스크를 종료 처리하고, 배정 시점 (s,a)에 지연 보상을 귀속시켜 Q-업데이트."""
        task = worker["task"]
        node_type = worker["type"]
        # 성공: 전체 실행시간. 실패(회수): 죽기 전까지 흘려보낸(낭비된) 시간.
        elapsed = worker["exec_time"] if success else max(0.0, worker["exec_time"] - max(0.0, worker["remaining"]))
        reward = self._terminal_reward(task, node_type, elapsed, success, evicted)
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
            self._requeue_or_deadletter(task)

    # ---------------------------------------------------------------- 1틱 전이
    def step(self):
        """
        한 틱(=DT초)을 전이한다. 실제 scheduler_daemon 1주기를 미러링:
        예산차감 → 진행/완료 → 회수폴링 → 태스크유입 → scale-in → 스케줄러 드레인(다중 결정).
        """
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

        # 4) 신규 태스크 유입 (실제 docker burst 4% / normal 18% 를 1초 주기로 보정 매핑 — workload.pretrain)
        is_burst = random.random() < _WL["pretrain"]["burst_prob"]
        is_normal = not is_burst and (random.random() < _WL["pretrain"]["normal_prob"])
        if is_burst:
            for _ in range(random.randint(5, 8)):
                self._generate_task()
        elif is_normal:
            self._generate_task()

        # 5) scale-in: 유휴 spot가 3초 지속되면 1대 축소 (실제 q_learning.py:39-53 미러)
        if self._has_idle_spot():
            self.idle_spot_duration += DT
        else:
            self.idle_spot_duration = 0.0
        if self.idle_spot_duration >= 3.0 and self._spot_count() > 0:
            self._scale_in_one_spot()
            self.idle_spot_duration = 0.0

        # 6) 스케줄러 드레인 — 한 틱에 유휴 워커가 있는 한 연속 배정. HOLD/SCALE는 틱 종료.
        self._decision_drain()

        done = self.virtual_budget <= 0.0
        return done

    def _decision_drain(self):
        # 실제 q_learning.py의 while-loop: ASSIGN이면 계속 드레인, HOLD/SCALE/큐빔/[3]-only면 종료.
        # 배정 가능한 워커·태스크 수로 상한이 잡히지만 안전 캡을 둔다.
        for _ in range(64):
            if not self.task_queue:
                break
            acts = self.available_actions()
            if acts == [3]:
                # 실제는 이 경우 HOLD 업데이트 없이 break한다.
                break
            state = self._get_state()
            action = self.agent.choose_action(state, acts)

            if action in (0, 1, 2):
                ntype = ["on_demand", "spot_a", "spot_b"][action]
                self._try_assign(ntype, state, action)
                continue  # 다음 태스크로 계속 드레인

            elif action == 3:
                # HOLD 즉시 보상 — reward_policy(유일 진실). 실제와 동일 수식.
                overdue = [self.sim_time - t["deadline"] for t in self.task_queue]
                reward = reward_policy.hold_reward(len(self.task_queue), overdue, self.agent.DELAY_PENALTY_WEIGHT)
                self.agent.update_q_value(state, action, reward, self._get_state())
                break

            elif action in (4, 5):
                self._scale_out(action, state)
                break

    def _try_assign(self, ntype, state, action):
        target = None
        for w in self.workers.values():
            if w["type"] == ntype and w["status"] == "IDLE":
                target = w
                break
        if target is None or not self.task_queue:
            # 불가능한 배정 — 실제는 penalty 없이 defer. reward_policy로 중립(0.0) 처리.
            self.agent.update_q_value(state, action, reward_policy.impossible_assign_reward(), self._get_state())
            return
        task = self.task_queue.pop(0)
        # 자원경합 OOM 사전 판정(동거 0). 실패 시 완료-시점 페널티 귀속 후 재큐/드롭.
        if FailureSimulator.check_oom(task["model_type"], task["task_id"], ntype, 0):
            reward = self._terminal_reward(task, ntype, 0.0, success=False, evicted=False)
            self.agent.update_q_value(state, action, reward, self._get_state())
            self._requeue_or_deadletter(task)
            return
        exec_time = self._compute_exec_time(task, ntype)
        target["status"] = "BUSY"
        target["task"] = task
        target["exec_time"] = exec_time
        target["remaining"] = exec_time
        target["s"] = state       # 지연 보상 귀속용 (배정 시점 상태/행동 기록)
        target["a"] = action

    def _scale_out(self, action, state):
        ntype = "spot_a" if action == 4 else "spot_b"
        urgent = self._urgent()
        cost_level = self._cost_level()

        # 한도 초과면 증설 불가 → 실패 보상
        if self._spot_count() >= MAX_SPOT_SCALE:
            reward = reward_policy.scale_reward(action, urgent, cost_level, scale_success=False)
            self.agent.update_q_value(state, action, reward, self._get_state())
            return

        # 실제 q_learning.py: q_len>=6 이고 여유가 있으면 2대 동시 증설
        q_len = len(self.task_queue)
        want = 2 if (q_len >= 6 and self._spot_count() < MAX_SPOT_SCALE - 1) else 1

        scaled = 0
        for _ in range(want):
            if self._spot_count() >= MAX_SPOT_SCALE:
                break
            if FailureSimulator.check_out_of_capacity(ntype):
                continue  # OutOfCapacity 거절
            self.spot_seq += 1
            wid = f"{'worker-2' if ntype == 'spot_a' else 'worker-3'}-{self.spot_seq}"
            self.workers[wid] = {"type": ntype, "status": "IDLE", "task": None,
                                 "remaining": 0.0, "exec_time": 0.0, "s": None, "a": None}
            scaled += 1

        reward = reward_policy.scale_reward(action, urgent, cost_level, scale_success=(scaled > 0), queue_backlog=(len(self.task_queue) >= 3))
        self.agent.update_q_value(state, action, reward, self._get_state())

    def _scale_in_one_spot(self):
        """유휴 spot 1대 축소(실제 scale-in). spot_a 우선 회수(실제 q_learning.py 순서와 동일)."""
        for pref in ("spot_a", "spot_b"):
            for wid, w in list(self.workers.items()):
                if w["type"] == pref and w["status"] == "IDLE":
                    del self.workers[wid]
                    return


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
        env.reset()
        for _ in range(400):  # 에피소드당 최대 400틱 (예산 고갈 동역학을 겪기에 충분한 길이)
            done = env.step()
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
    cost_model_path = _ec.COST_MODEL_PATH  # 유일 진실 경로(env_config)
    train_offline(episodes=episodes, cost_model_path=cost_model_path)


if __name__ == "__main__":
    run_offline_pretraining()
