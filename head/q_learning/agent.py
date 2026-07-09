# ==============================================================================
# WE-MEET: Q-Learning 비용/SLA 인지형 의사결정 에이전트 (head/q_learning.py)
# ==============================================================================

import os
import json
import random
import yaml

class QLearningAgent:
    """
    대기 태스크 큐 상태, 활성 노드 풀 상황, 잔여 가상 예산을 기반으로
    가장 비용 효율적이면서도 마감 시한(Deadline)을 지킬 수 있는 스케줄링 행동을 학습하는 에이전트.

    Attributes:
        alpha (float): 학습률 (Learning Rate).
        gamma (float): 미래 보상 할인율 (Discount Factor).
        epsilon (float): 현재 탐험율 (Exploration Rate).
        epsilon_min (float): 최소 탐험율 (Exploration Limit).
        decay_rate (float): 탐험율 감쇄율 (Decay rate per update step).
        q_table_path (str): Q-Table 저장 파일 경로.
        q_table (dict): 상태-행동 Q-Value 매핑 테이블.
        nodes_config (dict): 비용 모델 및 성능 격차 설정 딕셔너리.
        actions (list): 가용한 행동 공간 리스트.
    """
    def __init__(self, cost_model_path=None, q_table_path="q_table.json",
                 alpha=0.1, gamma=0.9, epsilon=1.0, epsilon_min=0.05, decay_rate=0.995):
        """
        QLearningAgent 인스턴스를 초기화하고 요금제 프로파일 및 Q-Table을 로드합니다.

        Args:
            cost_model_path (str, optional): YAML 비용 프로파일 파일 경로. Defaults to None.
            q_table_path (str, optional): 저장/로드할 Q-Table JSON 파일 경로. Defaults to "q_table.json".
            alpha (float, optional): 학습률. Defaults to 0.1.
            gamma (float, optional): 할인율. Defaults to 0.9.
            epsilon (float, optional): 초기 탐험율. Defaults to 1.0.
            epsilon_min (float, optional): 최소 탐험율. Defaults to 0.05.
            decay_rate (float, optional): 스텝별 Epsilon 감쇄 상수. Defaults to 0.995.
        """
        self.alpha = alpha       # 학습률 (Learning Rate)
        self.gamma = gamma       # 미래 보상 할인율 (Discount Factor)
        self.epsilon = epsilon   # 탐험율 (Exploration Rate)
        self.epsilon_min = epsilon_min  # 최소 탐험율
        self.decay_rate = decay_rate    # 감쇄율
        self.q_table_path = q_table_path
        if q_table_path == "q_table.json":
            # 사용자가 인지하는 공유 데이터 디렉토리(data/q_table.json) 경로로 영속화하여 호스트/컨테이너 간 일치시킵니다.
            self.q_table_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../data/q_table.json"))
        
        # Q-테이블 초기화: {(state_str): {action: q_value}}
        self.q_table = {}
        
        # 요금 및 자원 프로파일 로드
        self.nodes_config = {}
        if cost_model_path and os.path.exists(cost_model_path):
            try:
                with open(cost_model_path, 'r', encoding='utf-8') as f:
                    config = yaml.safe_load(f)
                    self.nodes_config = config.get("nodes", {})
            except Exception as e:
                print(f"[Q-Learning Agent] 설정 파일 로드 실패: {e}")
        
        # 기본 요금 정보 세팅 (cost_model.yaml 로드 실패 시 대체 대비)
        if not self.nodes_config:
            self.nodes_config = {
                "on_demand": {"cost_per_hour": 0.710, "gpu_scale_factor": 1.0},
                "spot_a": {"cost_per_hour": 0.220, "gpu_scale_factor": 0.6},
                "spot_b": {"cost_per_hour": 0.090, "gpu_scale_factor": 0.3}
            }

        # 보상 설계용 핵심 가중치 상수
        # 성공 보상은 정상적인(적절한 노드) 배정이 확실히 양(+)의 기대값을 갖도록 비용/makespan/회수 페널티보다
        # 충분히 크게 잡는다. 그렇지 않으면 에이전트가 어떤 배정도 하지 않고 HOLD만 반복하는 무행동 함정에 빠진다.
        self.SUCCESS_REWARD = 20.0
        self.COST_WEIGHT = 5000.0   
        self.DELAY_PENALTY_WEIGHT = 15.0
        self.EVICTION_PENALTY = 25.0
        # makespan 상시 감점 가중치. 
        # 기존에는 '느리다'는 이유로 상시 감점을 주어 Spot-B가 버려지는 원인이 되었습니다.
        # SLA를 어기지 않는 한, 느리더라도 싼 Spot-B를 쓰는 것이 똑똑한 것이므로 0으로 만듭니다.
        self.MAKESPAN_WEIGHT = 0.0

        # 행동 정의 (Action Space) - 6대 행동 확장
        # 0: ASSIGN_OD (On-demand 배정), 1: ASSIGN_SPOT_A (Spot-A 배정), 2: ASSIGN_SPOT_B (Spot-B 배정)
        # 3: HOLD (대기열 지연 보류), 4: SCALE_OUT_SPOT_A (Spot-A 증설), 5: SCALE_OUT_SPOT_B (Spot-B 증설)
        self.actions = [0, 1, 2, 3, 4, 5]
        
        # Q-테이블 자동 로드
        self.load_q_table()

    @staticmethod
    def state_to_str(state):
        """상태 튜플을 Q-Table key용 문자열로 변환하는 공통 정적 메소드입니다."""
        return "_".join(str(x) for x in state)

    def _state_to_str(self, state):
        """
        상태 튜플 (t_profile, w_active, p_spot, b_level)을 Q-Table key용 문자열로 변환합니다.

        Args:
            state (tuple): (대기열 프로파일, 활성비트맵, 요금위험도, 예산 수준) 형태의 튜플.

        Returns:
            str: "프로파일_비트맵_요금위험도_예산수준" 형식의 문자열 키.
        """
        return self.state_to_str(state)

    def get_q_value(self, state, action):
        """
        특정 상태와 행동에 매핑된 Q-값을 반환합니다.

        Args:
            state (tuple): 조회 대상 상태 튜플.
            action (int): 조회 대상 행동 정수 ID.

        Returns:
            float: Q-Table 상에 기록된 Q-Value (미등장 상태인 경우 0.0으로 신규 생성).
        """
        state_str = self._state_to_str(state)
        if state_str not in self.q_table:
            # 상태가 처음 등록되는 경우 모든 행동의 Q-값을 0.0으로 초기화
            self.q_table[state_str] = {a: 0.0 for a in self.actions}
        return self.q_table[state_str].get(action, 0.0)

    def choose_action(self, state, available_actions=None):
        """
        Epsilon-Greedy 탐색 정책을 활용하여 행동을 선정합니다.

        Args:
            state (tuple): 현재 환경의 상태 튜플.
            available_actions (list, optional): 현재 시점에 가용한 행동 공간 목록. Defaults to None.

        Returns:
            int: 결정된 행동 정수 ID.
        """
        if available_actions is None:
            available_actions = self.actions

        # 1. 탐험 (Exploration): 설정한 탐험 확률(epsilon) 이하일 경우 랜덤 행동을 선택
        if random.random() < self.epsilon:
            return random.choice(available_actions)
        
        # 2. 이용 (Exploitation): 가장 높은 Q-값을 가진 행동을 탐색
        state_str = self._state_to_str(state)
        if state_str not in self.q_table:
            self.q_table[state_str] = {a: 0.0 for a in self.actions}
            
        q_vals = self.q_table[state_str]
        
        # 가용한 행동들 중에서 최댓값을 고릅니다.
        best_val = -float('inf')
        best_actions = []
        
        for action in available_actions:
            q_val = q_vals.get(action, 0.0)
            if q_val > best_val:
                best_val = q_val
                best_actions = [action]
            elif q_val == best_val:
                best_actions.append(action)
                
        # 최대 Q-값이 중복될 경우 랜덤하게 선정하여 편향을 배제
        return random.choice(best_actions)

    def update_q_value(self, state, action, reward, next_state, next_available_actions=None):
        """
        Q-Learning 학습 핵심 갱신 알고리즘 (Bellman Equation 적용) 및 Epsilon 감쇄를 연동합니다.

        Args:
            state (tuple): 이전 상태 튜플.
            action (int): 이전 행동 ID.
            reward (float): 획득한 보상값.
            next_state (tuple): 전이된 다음 상태 튜플.
            next_available_actions (list, optional): 다음 상태에서 가용한 행동 리스트. Defaults to None.
        """
        if next_available_actions is None:
            next_available_actions = self.actions

        # 현재 상태의 Q-값 가져오기
        current_q = self.get_q_value(state, action)
        
        # 다음 상태에서 취할 수 있는 최적의 행동의 Q-값 예측
        max_next_q = -float('inf')
        for next_act in next_available_actions:
            next_q = self.get_q_value(next_state, next_act)
            if next_q > max_next_q:
                max_next_q = next_q
                
        if max_next_q == -float('inf'):
            max_next_q = 0.0

        # Bellman Equation 수식을 사용하여 업데이트 계산
        new_q = current_q + self.alpha * (reward + self.gamma * max_next_q - current_q)
        
        # Q-테이블 갱신
        state_str = self._state_to_str(state)
        self.q_table[state_str][action] = new_q

        # Epsilon 감쇄 적용
        if self.epsilon > self.epsilon_min:
            self.epsilon = max(self.epsilon_min, self.epsilon * self.decay_rate)

    def calculate_reward(self, success, execution_time, worker_type, delay_time, deadline_exceeded, current_model=None, co_scheduled_models=None, evicted=False):
        """
        보상 함수(Reward Function) 수식 모델 구현.

        Args:
            success (bool): 태스크 실행 성공 완료 여부.
            execution_time (float): 실제 연산 수행 소요 시간(회수 실패 시 낭비된 시간).
            worker_type (str): 연산에 사용된 워커 타입 ("on_demand" / "spot_a" / "spot_b").
            delay_time (float): SLA 마감 기한 초과 지연 시간.
            deadline_exceeded (bool): SLA 데드라인 초과 여부.
            current_model (str, optional): 현재 배치된 모델명.
            co_scheduled_models (list, optional): 함께 배치된 모델명 리스트.
            evicted (bool): 스팟 강제 회수(Eviction)로 인해 실패했는지 여부.

        Returns:
            float: 산출된 보상(Reward) 스칼라 값.
        """
        reward = 0.0

        # 1. SLA 완료 보너스
        if success:
            reward += self.SUCCESS_REWARD

        # 2. 실행 비용 감점 (Cost_run = Cost_worker * Time_execution)
        # 시간당 요금 모델을 초 단위로 환산하여 감산 적용
        cost_profile = self.nodes_config.get(worker_type, {})
        cost_per_hour = cost_profile.get("cost_per_hour", 0.0)
        execution_cost = cost_per_hour * (execution_time / 3600.0)

        # 비용 가중치를 곱하여 보상에서 차감 (예산 절약 유도)
        reward -= self.COST_WEIGHT * execution_cost

        # 2-1. makespan 상시 감점: 데드라인 초과 여부와 무관하게 '느림' 자체에 대가를 부과한다.
        #      → 싸지만 느린 Spot-B가 비용 항에서만 이득을 보던 편향을 상쇄.
        reward -= self.MAKESPAN_WEIGHT * execution_time

        # 3. 지연 페널티 (SLA 마감 기한 초과 시 초당 페널티 감점)
        if deadline_exceeded:
            reward -= self.DELAY_PENALTY_WEIGHT * delay_time

        # 3-1. 회수(Eviction) 페널티: 스팟 강제 회수로 태스크가 죽으면 강한 벌점.
        #      성공 보상(+10)을 상회하여, 회수 위험이 큰 배치(느린 노드×위험구간)를 학습으로 회피하게 한다.
        if evicted:
            reward -= self.EVICTION_PENALTY

        # 4. 이종 모형 융합 배치 (Co-scheduling) 조화도에 따른 보상/페널티 추가 (자율 융합 유도)
        if current_model and co_scheduled_models:
            is_current_mem_bound = current_model.upper() in ["RNN", "LSTM"]
            
            for other_model in co_scheduled_models:
                is_other_mem_bound = other_model.upper() in ["RNN", "LSTM"]
                
                # 서로 다른 리소스 성격(컴퓨트 바운드 CNN vs 메모리 바운드 RNN/LSTM)이 융합 탑재된 경우 가산 보상
                if is_current_mem_bound != is_other_mem_bound:
                    reward += 0.15
                else:
                    # 동일 리소스 성격이 편중되어 자원 경합을 유발한 경우 페널티 감점
                    reward -= 0.20
            
        return reward

    def save_q_table(self):
        """학습된 Q-Table 데이터를 로컬 JSON 파일로 영구 보존합니다.
        기존 파일이 있는 경우 머지(Merge)하여 메모리에 없는 상태들의 정보가 유실되지 않도록 보존합니다.
        또한, 탐험율(Epsilon) 값을 별도의 메타데이터 파일에 영속화합니다.
        파일 쓰기 중 중단/크래시 시 손상을 막기 위해 임시 파일 생성 후 파일 교체(Atomic Write) 기법을 사용합니다.
        """
        try:
            # 1. 기존 파일 로드 및 머지
            existing_table = {}
            if os.path.exists(self.q_table_path) and os.path.getsize(self.q_table_path) > 0:
                try:
                    with open(self.q_table_path, 'r', encoding='utf-8') as f:
                        existing_table = json.load(f)
                except Exception:
                    existing_table = {}
            
            # 2. 메모리 Q-Table 데이터를 문자열 키로 변환하여 병합 (JSON은 키가 문자열이어야 함)
            for state_str, actions_dict in self.q_table.items():
                if state_str not in existing_table:
                    existing_table[state_str] = {}
                for act, q_val in actions_dict.items():
                    existing_table[state_str][str(act)] = q_val
            
            # 3. 임시 파일에 최종 병합본을 쓰고 원자적으로 덮어쓰기 (Atomic Write)
            temp_path = self.q_table_path + ".tmp"
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(existing_table, f, indent=4)
            os.replace(temp_path, self.q_table_path)
                
            # 4. 탐험율(Epsilon) 임시 파일 생성 후 원자적 덮어쓰기
            metadata_path = self.q_table_path.replace(".json", "_metadata.json")
            temp_meta_path = metadata_path + ".tmp"
            with open(temp_meta_path, 'w', encoding='utf-8') as f:
                json.dump({"epsilon": self.epsilon}, f, indent=4)
            os.replace(temp_meta_path, metadata_path)
        except Exception as e:
            print(f"[Q-Learning Agent] Q-Table 저장 오류: {e}")

    def load_q_table(self):
        """로컬 저장소로부터 기존에 학습되어 저장된 Q-Table 및 Epsilon 값을 불러옵니다."""
        if os.path.exists(self.q_table_path):
            try:
                if os.path.getsize(self.q_table_path) == 0:
                    raw_q_table = {}
                else:
                    with open(self.q_table_path, 'r', encoding='utf-8') as f:
                        raw_q_table = json.load(f)
            except (json.JSONDecodeError, ValueError) as e:
                print(f"[Q-Learning Agent 경고] Q-Table 파일이 비어 있거나 손상되어 빈 딕셔너리로 초기화합니다: {e}")
                raw_q_table = {}
            
            try:
                # 중복 저장 방지를 위해 JSON 문자열 액션 키를 정수(int) 키로 명시적 형변환하여 로드
                self.q_table = {}
                for state_str, actions_dict in raw_q_table.items():
                    self.q_table[state_str] = {int(a): float(q) for a, q in actions_dict.items()}
                
                # Epsilon 값 메타데이터 복구
                metadata_path = self.q_table_path.replace(".json", "_metadata.json")
                if os.path.exists(metadata_path):
                    try:
                        with open(metadata_path, 'r', encoding='utf-8') as f:
                            meta = json.load(f)
                            self.epsilon = meta.get("epsilon", self.epsilon)
                    except Exception:
                        pass
                
                # Q-Table이 성공적으로 로드된 경우 기학습된 지식을 활용하기 위해 추론 모드일 때만 탐험율을 최소치로 즉시 전환
                if self.q_table:
                    import head.state as gcs_state
                    if not getattr(gcs_state, "Q_LEARNING_TRAINING_MODE", True):
                        self.epsilon = self.epsilon_min
                    
                print(f"[Q-Learning Agent] Q-Table 로드 성공. (보존된 상태수: {len(self.q_table)}) | 탐험율(Epsilon)을 {self.epsilon}으로 설정합니다.")
            except Exception as e:
                print(f"[Q-Learning Agent] Q-Table 로드 실패: {e}")
        else:
            print("[Q-Learning Agent] 신규 Q-Table을 생성합니다.")
