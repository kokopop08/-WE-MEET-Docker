# ==============================================================================
# WE-MEET: Q-Learning 오프라인 고속 사전 학습 시뮬레이터 (pretrain.py)
# ==============================================================================

import os
import sys
import random

# 실행 시 프로젝트 루트 디렉토리를 sys.path에 추가하여 모듈을 찾도록 설정
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from head.q_learning.agent import QLearningAgent

def run_offline_pretraining(episodes=25000):
    print("=== [Pretrain Simulator] 오프라인 고속 Q-Learning 에이전트 사전 훈련을 개시합니다. ===")
    
    # cost_model.yaml 절대 경로 탐색
    cost_model_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../common/cost_model.yaml'))
    
    # Q-Learning 에이전트 인스턴스 초기화 (훈련을 위해 Epsilon을 1.0으로 강제 세팅하여 활발히 탐험)
    agent = QLearningAgent(
        cost_model_path=cost_model_path, 
        q_table_path="q_table.json",
        alpha=0.1,
        gamma=0.9,
        epsilon=1.0,
        epsilon_min=0.05,
        decay_rate=0.9995  # 25,000 에피소드에 걸쳐 부드럽게 감쇄
    )
    
    # 가용 액션 공간 정의
    # 0: ASSIGN_OD, 1: ASSIGN_SPOT_A, 2: ASSIGN_SPOT_B, 3: HOLD, 4: SCALE_OUT_SPOT_A, 5: SCALE_OUT_SPOT_B
    actions = [0, 1, 2, 3, 4, 5]
    
    # CSV 로깅용 데이터 수집 리스트
    pretrain_logs = []
    
    for ep in range(episodes):
        # 1. 초기 임의의 상태 생성
        w_mix = random.choice([1, 2, 3])  # 비어있는 0 상태는 배정할 필요가 없으므로 제외
        a_mix = random.randint(0, 7)
        u_sla = random.choice([0, 1])
        c_level = random.choice([0, 1])
        
        state = (w_mix, a_mix, u_sla, c_level)
        
        # 2. 에이전트의 액션 선택
        action = agent.choose_action(state, available_actions=actions)
        
        # 3. 가상 환경의 1-Step 물리 전이 및 보상 계산
        reward = 0.0
        
        # nodes_config가 정상 로드되었으므로 이를 활용하여 기본 요금 파싱 (없다면 YAML 폴백 활용)
        cost_od = agent.nodes_config.get("on_demand", {}).get("cost_per_hour", 0.710)
        cost_spot_a = agent.nodes_config.get("spot_a", {}).get("cost_per_hour", 0.220)
        cost_spot_b = agent.nodes_config.get("spot_b", {}).get("cost_per_hour", 0.090)
        
        # 3-1. 클러스터 총 비용 수준(c_level)에 따른 보상 제약
        if c_level == 1:  # 시간당 소모 비용(Burn Rate)이 높은 상황
            if action in [0, 4]:  # 고비용 OD 배정 또는 Spot-A 증설
                reward -= 3.0  # 고비용 지출 페널티
            elif action in [2, 5]:  # 초저렴 Spot-B 배정 및 증설
                reward -= 1.0  # 저비용 자원이나 Burn Rate가 높으므로 약한 페널티
            elif action == 3:  # HOLD 보류
                reward += 1.0  # 지출 보류 인센티브
        else:  # 시간당 소모 비용이 낮은 상황 (c_level == 0)
            if action in [0, 1, 4]:
                reward += 3.0  # 적극적인 자원 할당/증설 인센티브
        
        # 3-2. SLA 임박도(u_sla)에 따른 보상 제약
        if u_sla == 1:  # 마감 임박 상황
            if action == 3:  # HOLD 지연 보류 선택 시 에이징 대기 페널티
                reward -= 15.0  # 초강력 기아 및 지연 페널티 부과 (HOLD 원천 기피 유도)
            elif action in [0, 1]:  # 즉시 고성능 자원(OD, Spot-A)에 배정
                reward += 6.0  # SLA 수렴 보너스 (성능 자원 활용 극대화)
            elif action == 2:  # 저성능 Spot-B 배정
                reward -= 5.0  # 마감이 급한데 느린 노드를 써서 페널티 대폭 강화 (Spot-B 편향 탈피의 핵심 트리거)
            elif action == 4:  # Spot-A 증설
                reward += 4.0  # 마감 임박 시 성능형 자원 증설 강력 보상
            elif action == 5:  # Spot-B 증설
                reward += 1.0  # 저성능 증설은 낮은 보너스 부여
        else:  # 마감 여유 상황
            if action == 3:  # HOLD 보류
                reward += 1.0  # 불필요한 돈을 쓰지 않고 숨을 골랐으므로 약간의 보상
            elif action in [0, 4]:  # 불필요한 OD 배정 또는 과잉 증설
                reward -= 1.5  # 과도 예산 낭비 감점
        
        # 3-3. 가용 자원 토폴로지(a_mix)와의 액션 정합성 체크
        # 예: 가용 비트맵 상에 해당 자원이 없는데 ASSIGN을 때린 경우의 패널티
        is_od_idle = (a_mix & 1) > 0
        is_spot_a_idle = (a_mix & 2) > 0
        is_spot_b_idle = (a_mix & 4) > 0
        
        if action == 0 and not is_od_idle:  # OD 가용치 없는데 OD 배정
            reward -= 4.0
        elif action == 1 and not is_spot_a_idle:  # Spot-A 가용치 없는데 Spot-A 배정
            reward -= 4.0
        elif action == 2 and not is_spot_b_idle:  # Spot-B 가용치 없는데 Spot-B 배정
            reward -= 4.0
            
        # 4. 다음 상태(Next State) 전이
        w_mix_next = random.choice([0, 1, 2, 3])
        a_mix_next = random.randint(0, 7)
        u_sla_next = random.choice([0, 1])
        c_level_next = random.choice([0, 1])
        next_state = (w_mix_next, a_mix_next, u_sla_next, c_level_next)
        
        # 5. Q-Value 업데이트 및 Epsilon 감쇄
        agent.update_q_value(state, action, reward, next_state, next_available_actions=actions)
        
        # 6. CSV 로깅용 데이터 수집 (유니파이드 헬퍼 활용)
        state_str = QLearningAgent.state_to_str(state)
        pretrain_logs.append([ep + 1, state_str, action, round(reward, 4), round(agent.epsilon, 6)])
        
    # 7. 완성된 Q-Table 저장
    agent.save_q_table()
    print(f"=== [Pretrain Simulator] 사전 학습 성공 완료! 수렴된 상태 수: {len(agent.q_table)} ===")
    print(f"=== [Pretrain Simulator] 최종 감쇄된 Epsilon: {agent.epsilon:.4f} ===")
    
    # 8. 학습 과정 히스토리 CSV 파일 출력 저장
    csv_file = "data/pretrain_history.csv"
    try:
        os.makedirs(os.path.dirname(csv_file), exist_ok=True)
        import csv
        with open(csv_file, mode="w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            # 헤더 작성
            writer.writerow(["episode", "state", "action", "reward", "epsilon"])
            # 데이터 로깅
            writer.writerows(pretrain_logs)
        print(f"=== [Pretrain Simulator] 에피소드 반복별 학습 데이터를 CSV로 성공적으로 저장했습니다: {csv_file} ===")
    except Exception as e:
        print(f"[Pretrain Simulator 경고] 사전 학습 CSV 저장 중 오류 발생: {e}")

if __name__ == "__main__":
    run_offline_pretraining()
