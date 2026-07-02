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
    
    # Q-Learning 에이전트 인스턴스 초기화 (훈련을 위해 Epsilon을 1.0으로 강제 세팅하여 활발히 탐험)
    agent = QLearningAgent(
        cost_model_path=None, 
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
    
    # 가상의 환경 보상 파라미터 모사
    # 상태 포맷: (w_mix, a_mix, u_sla, b_avail)
    # w_mix: 0 (비어있음), 1 (CNN), 2 (RNN/LSTM), 3 (혼재)
    # a_mix: 0~7 (OD=1, SpotA=2, SpotB=4 가용 비트맵 조합)
    # u_sla: 0 (여유), 1 (임박)
    # b_avail: 0 (위기), 1 (충분)
    
    for ep in range(episodes):
        # 1. 초기 임의의 상태 생성
        w_mix = random.choice([1, 2, 3])  # 비어있는 0 상태는 배정할 필요가 없으므로 제외
        a_mix = random.randint(0, 7)
        u_sla = random.choice([0, 1])
        b_avail = random.choice([0, 1])
        
        state = (w_mix, a_mix, u_sla, b_avail)
        
        # 2. 에이전트의 액션 선택
        action = agent.choose_action(state, available_actions=actions)
        
        # 3. 가상 환경의 1-Step 물리 전이 및 보상 계산
        reward = 0.0
        
        # 기본 요금 설정
        cost_od = 1.0
        cost_spot_a = 0.4
        cost_spot_b = 0.2
        
        # 3-1. 예산 가용성 팩터(b_avail)에 따른 보상 제약
        if b_avail == 0:  # 예산 위기 상황
            if action in [0, 4]:  # 고비용 OD 배정 또는 Spot-A 증설
                reward -= 5.0  # 강력한 요금 초과 페널티
            elif action in [2, 5]:  # 초저렴 Spot-B 배정 및 증설
                reward += 2.0  # 알뜰 의사결정 인센티브
            elif action == 3:  # HOLD 보류
                reward += 1.0  # 가격 지출을 방지했으므로 약간의 보상
        else:  # 예산 풍족 상황
            if action in [0, 1, 4]:
                reward += 1.5  # 가속 성능 활용 인센티브
        
        # 3-2. SLA 임박도(u_sla)에 따른 보상 제약
        if u_sla == 1:  # 마감 임박 상황
            if action == 3:  # HOLD 지연 보류 선택 시 에이징 대기 페널티
                reward -= 10.0  # 초강력 기아 및 지연 페널티 부과!
            elif action in [0, 1]:  # 즉시 고성능 자원(OD, Spot-A)에 배정
                reward += 4.0  # SLA 수렴 보너스
            elif action == 2:  # 저성능 Spot-B 배정
                reward -= 2.0  # 마감이 급한데 느린 노드를 써서 페널티
            elif action in [4, 5]:  # 증설
                reward += 2.0  # 동적 대응 보너스
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
        b_avail_next = random.choice([0, 1])
        next_state = (w_mix_next, a_mix_next, u_sla_next, b_avail_next)
        
        # 5. Q-Value 업데이트 및 Epsilon 감쇄
        agent.update_q_value(state, action, reward, next_state, next_available_actions=actions)
        
    # 6. 완성된 Q-Table 저장
    agent.save_q_table()
    print(f"=== [Pretrain Simulator] 사전 학습 성공 완료! 수렴된 상태 수: {len(agent.q_table)} ===")
    print(f"=== [Pretrain Simulator] 최종 감쇄된 Epsilon: {agent.epsilon:.4f} ===")

if __name__ == "__main__":
    run_offline_pretraining()
