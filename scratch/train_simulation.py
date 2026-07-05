# ==============================================================================
# WE-MEET: Q-Learning 오프라인 시뮬레이션 학습기 (scratch/train_simulation.py)
# ==============================================================================

import os
import sys
import random
import time
import json

# 프로젝트 루트 디렉토리를 path에 추가하여 head 패키지 임포트 지원
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from head.q_learning import QLearningAgent

class SimulatedEnvironment:
    """
    큐 길이, 활성 노드 수, 잔여 예산을 포함하는 마스터 노드 스케줄링 환경의
    상태 전이 및 태스크 수명 주기를 의사 수학적으로 모사하는 학습용 시뮬레이션 환경.
    """
    def __init__(self, cost_model_path=None):
        self.agent = QLearningAgent(cost_model_path=cost_model_path)
        self.reset()

    def reset(self):
        """환경 상태를 초기화합니다."""
        self.virtual_budget = 100.0
        self.task_queue = []
        self.task_counter = 0
        
        # 활성 워커 상태 모사
        # OD(worker-1)는 항상 1대 활성, Spot-A는 0~3대 동적 스케일링
        self.worker_1_status = "IDLE"  # On-demand
        self.worker_2_scale = 0       # Spot-A 대수
        self.worker_2_status = []     # 각 Spot-A 워커의 상태 ["IDLE", "BUSY", ...]
        
        # 가상 진행 중인 태스크들 {worker_name: {"task": task_dict, "remaining_time": float}}
        self.running_tasks = {}
        
        # 초기 태스크 적재 (5~8개 생성)
        for _ in range(random.randint(5, 8)):
            self._generate_task()
            
        return self._get_state()

    def _generate_task(self):
        self.task_counter += 1
        model = random.choice(["CNN", "RNN", "LSTM"])
        epochs = random.randint(5, 10)
        timeout = random.randint(25, 45)
        self.task_queue.append({
            "task_id": f"sim-task-{self.task_counter:04d}",
            "model_type": model,
            "epochs": epochs,
            "deadline": time.time() + timeout,
            "enqueue_time": time.time()
        })

    def _get_state(self):
        q_len = min(len(self.task_queue), 10)
        
        w1_act = 1  # On-demand는 항상 존재
        w2_act = 1 if self.worker_2_scale > 0 else 0
        active_bitmap = (w1_act * 1) + (w2_act * 2)
        
        budget_level = 0 if self.virtual_budget < 20.0 else (1 if self.virtual_budget < 70.0 else 2)
        return (q_len, active_bitmap, budget_level)

    def step(self, action):
        """
        결정된 행동을 환경에 적용하고 상태 전이와 보상을 도출합니다.

        Args:
            action (int): 에이전트 행동 (0: OD 배정, 1: Spot 배정, 2: HOLD, 3: SCALE_OUT)

        Returns:
            tuple: (다음 상태, 즉각 보상, 종료 여부, 메타데이터)
        """
        reward = 0.0
        done = False
        info = {"msg": ""}
        
        # 1. 진행 중인 작업들의 가상 시간 흐름 업데이트 (1초 경과 모사)
        finished_workers = []
        for worker, task_info in list(self.running_tasks.items()):
            task_info["remaining_time"] -= 1.0
            if task_info["remaining_time"] <= 0.0:
                finished_workers.append(worker)
                
        # 완료된 작업 피드백 및 보상 산출
        for worker in finished_workers:
            task_data = self.running_tasks[worker]
            task = task_data["task"]
            worker_type = "on_demand" if worker == "worker-1" else "spot_a"
            
            # 실제 실행 성공 완료 처리 (시뮬레이션이므로 92% 확률로 성공, 8% OOM 모사)
            success = random.random() > 0.08 if task["model_type"] == "LSTM" else True
            
            # 가상 시간 및 비용 정산
            exec_time = task_data["exec_time"]
            cost_profile = self.agent.nodes_config.get(worker_type, {"cost_per_hour": 1.0 if worker_type == "on_demand" else 0.4})
            cost_per_hour = cost_profile.get("cost_per_hour", 1.0)
            task_cost = cost_per_hour * (exec_time / 3600.0)
            self.virtual_budget -= task_cost
            
            # 보상 산출
            end_time = time.time() + exec_time
            delay_time = max(0.0, end_time - task["deadline"])
            deadline_exceeded = end_time > task["deadline"]
            
            task_reward = self.agent.calculate_reward(
                success=success,
                execution_time=exec_time,
                worker_type=worker_type,
                delay_time=delay_time,
                deadline_exceeded=deadline_exceeded
            )
            reward += task_reward
            
            # 워커 상태 복구
            if worker == "worker-1":
                self.worker_1_status = "IDLE"
            else:
                idx = int(worker.split("-")[-1]) - 1
                if idx < len(self.worker_2_status):
                    self.worker_2_status[idx] = "IDLE"
                    
            del self.running_tasks[worker]
            
            # 태스크 실패 시 큐 재배정 (Task Lineage 모사)
            if not success:
                self.task_queue.insert(0, task)

        # 2. 새로운 태스크 40% 확률로 자동 생성 유입
        if random.random() < 0.4:
            self._generate_task()

        # 3. 에이전트 액션 처리
        # 0: OD 배정
        if action == 0:
            if self.worker_1_status == "IDLE" and self.task_queue:
                task = self.task_queue.pop(0)
                self.worker_1_status = "BUSY"
                # OD 연산 시간 모사 (기본 1Epoch당 약 3.0초)
                exec_time = task["epochs"] * 3.0
                self.running_tasks["worker-1"] = {
                    "task": task,
                    "remaining_time": exec_time,
                    "exec_time": exec_time
                }
                info["msg"] = f"Assigned {task['task_id']} to OD Worker"
            else:
                # 불가능한 액션을 취했을 때의 경미한 감점
                reward -= 1.0

        # 1: Spot 배정
        elif action == 1:
            # IDLE 상태인 Spot 워커 색출
            idle_spot_idx = -1
            for idx, status in enumerate(self.worker_2_status):
                if status == "IDLE":
                    idle_spot_idx = idx
                    break
                    
            if idle_spot_idx != -1 and self.task_queue:
                task = self.task_queue.pop(0)
                worker_name = f"worker-2-{idle_spot_idx + 1}"
                self.worker_2_status[idle_spot_idx] = "BUSY"
                # Spot 연산 시간 모사 (성능 계수 0.6 적용되어 OD보다 1.67배 느림)
                exec_time = (task["epochs"] * 3.0) / 0.6
                self.running_tasks[worker_name] = {
                    "task": task,
                    "remaining_time": exec_time,
                    "exec_time": exec_time
                }
                info["msg"] = f"Assigned {task['task_id']} to Spot Worker {worker_name}"
            else:
                # 자원이 없거나 큐가 비어있는데 할당하려 한 페널티
                reward -= 2.0

        # 2: HOLD (대기 및 지연)
        elif action == 2:
            # 큐 적재된 태스크들에 대해 대기 페널티를 누적 부과
            reward -= 0.1 * len(self.task_queue)
            info["msg"] = "HOLD action"

        # 3: SCALE_OUT (Spot 증설)
        elif action == 3:
            if self.worker_2_scale < 3:
                self.worker_2_scale += 1
                self.worker_2_status.append("IDLE")
                reward -= 0.5  # 인프라 기동 비용 감점
                info["msg"] = f"Scaled-out Spot Worker (Current scale: {self.worker_2_scale})"
            else:
                reward -= 1.5  # 스케일 한도 초과 기동 페널티

        # 가상 예산 완전 고갈 시 에피소드 종료 조건 판정
        if self.virtual_budget <= 0.0:
            reward -= 50.0  # 파산 페널티
            done = True

        return self._get_state(), reward, done, info

def train_offline(epochs=20000, cost_model_path=None):
    """
    시뮬레이션 환경을 통해 Q-Learning 에이전트를 오프라인으로 훈련시킵니다.
    """
    print("=== [Q-Learning] 오프라인 시뮬레이션 사전 훈련을 시작합니다. ===")
    env = SimulatedEnvironment(cost_model_path=cost_model_path)
    agent = env.agent
    
    # 훈련용 하이퍼파라미터 세팅 (시뮬레이션 상에서 감쇄가 일어나도록 설정)
    agent.epsilon = 1.0
    agent.epsilon_min = 0.05
    agent.decay_rate = 0.99995  # 수렴을 위한 부드러운 감쇄
    
    cumulative_rewards = 0.0
    success_episodes = 0
    
    for epoch in range(1, epochs + 1):
        state = env.reset()
        episode_reward = 0.0
        step_count = 0
        
        while step_count < 100:  # 에피소드당 최대 100틱 제한
            step_count += 1
            
            # 가용 행동 필터링
            available_actions = [2]  # HOLD는 항상 가능
            if env.worker_1_status == "IDLE":
                available_actions.append(0)
            if "IDLE" in env.worker_2_status:
                available_actions.append(1)
            if env.worker_2_scale < 3:
                available_actions.append(3)
                
            action = agent.choose_action(state, available_actions)
            next_state, reward, done, _ = env.step(action)
            
            # GCS 상태에 맞춘 가용 행동 리스트
            next_available = [2]
            if env.worker_1_status == "IDLE":
                next_available.append(0)
            if "IDLE" in env.worker_2_status:
                next_available.append(1)
            if env.worker_2_scale < 3:
                next_available.append(3)
                
            agent.update_q_value(state, action, reward, next_state, next_available)
            state = next_state
            episode_reward += reward
            
            if done:
                break
                
        cumulative_rewards += episode_reward
        if env.virtual_budget > 0.0:
            success_episodes += 1
            
        if epoch % 2000 == 0:
            avg_reward = cumulative_rewards / 2000
            success_rate = (success_episodes / 2000) * 100.0
            print(f"Episode {epoch:5d}/{epochs} | Avg Reward: {avg_reward:7.2f} | SLA/Budget Success: {success_rate:5.1f}% | Current Epsilon: {agent.epsilon:.4f}")
            cumulative_rewards = 0.0
            success_episodes = 0
            
    # 완성된 Q-Table 저장
    agent.save_q_table()
    print("=== [Q-Learning] 오프라인 사전 훈련 완료 및 q_table.json 저장 성공. ===")

if __name__ == '__main__':
    COST_MODEL_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../common/cost_model.yaml'))
    train_offline(epochs=20000, cost_model_path=COST_MODEL_PATH)
