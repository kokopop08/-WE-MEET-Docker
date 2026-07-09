# ==============================================================================
# WE-MEET: Q-Learning 비-ASSIGN 행동 보상 단일 산출 모듈
# (head/q_learning/reward_policy.py)
#
# [존재 이유]
# HOLD·SCALE_OUT·불가능배정의 보상 수식이 실제 스케줄러(head/scheduler/q_learning.py)와
# 오프라인 시뮬레이터(head/q_learning/pretrain.py)에 복붙되어 있어, 한쪽만 바뀌면 두 세계의
# 학습 경제(economics)가 조용히 어긋났다. 실제로 SCALE 보상이 부호까지 달라져(실제는
# 긴급+저비용 증설을 양(+)으로 보상, sim은 항상 음(-)) sim이 만든 warm-start가 구조적으로
# "증설 회피" 정책을 학습하는 문제가 있었다.
#
# state_features.py가 상태 인코딩을 단일 함수로 통일한 것과 동일하게, 이 모듈이 비-ASSIGN
# 행동의 보상 수식을 유일 진실로 보유한다. 실제 경로와 시뮬레이터가 모두 이 함수를 호출한다.
#
# [주의] ASSIGN(0/1/2) 행동의 지연 보상은 태스크 완료/회수 시점에 QLearningAgent.calculate_reward
#        가 담당한다(이미 두 경로가 공유). 이 모듈은 그 외 행동(HOLD/SCALE/불가능배정)만 다룬다.
#
# 모든 함수는 순수 함수다: 전역 상태·시각·환경에 의존하지 않고, 이미 계산된 원시값만 받는다.
# (cost_level·urgent 등 상태 파생은 호출자 책임 — state_features.compute_state와 동일한 규약.)
# ==============================================================================


def hold_reward(q_len, overdue_seconds_list, delay_penalty_weight):
    """
    HOLD(대기열 지연 보류) 행동의 즉시 보상.

    무행동 함정(HOLD만 반복) 방지를 위해 대기열이 밀려 있으면 감점하고, 큐가 거의 비었을 때만
    소폭 양(+)을 준다. 마감 초과 태스크가 있으면 초과분에 비례해 추가 감점한다.

        reward = 1.0 - 0.5 * q_len - Σ(over * delay_penalty_weight * 0.2)

    Args:
        q_len (int): 현재 대기열 크기.
        overdue_seconds_list (list[float]): 대기열 각 태스크의 '마감 초과 시간(초)'. 초과하지 않은
                                            태스크는 0 이하 값이거나 목록에서 제외되어 있어도 된다
                                            (0 이하는 무시된다).
        delay_penalty_weight (float): 지연 페널티 가중치(agent.DELAY_PENALTY_WEIGHT).

    Returns:
        float: HOLD 보상.
    """
    hold_penalty = 0.0
    for over in overdue_seconds_list:
        if over > 0.0:
            hold_penalty += over * delay_penalty_weight * 0.2
    return 1.0 - 0.5 * q_len - hold_penalty


def scale_reward(action, urgent, cost_level, scale_success):
    """
    SCALE_OUT 행동(4=Spot-A 증설, 5=Spot-B 증설)의 즉시 보상.

    증설은 그 자체로 요금 부담(감점)이지만, 마감이 임박(urgent)했고 아직 총요금이 낮은(cost_level=0)
    국면이면 빠른 Spot-A 증설을 양(+)으로 보상한다 → "필요할 때 증설"을 학습. 반대로 고비용 국면
    (cost_level=1: 총 시간당요금 > $9)에서는 추가 증설을 강하게 감점한다. 증설 실패(물리 자원 부족
    또는 OutOfCapacity)는 강한 벌점(-10).

    수식(head/scheduler/q_learning.py의 온라인 학습 블록과 완전히 동일):
        action 4 (Spot-A): success → (4.0 if urgent else -1.5) - 3.5,  cost_level=1 이면 -3.0 else +3.0
        action 5 (Spot-B): success → (1.0 if urgent else  0.0) - 2.0,  cost_level=1 이면 -2.0 else +1.0
        실패(둘 다)       → -10.0

    Args:
        action (int): 4(Spot-A 증설) 또는 5(Spot-B 증설).
        urgent (bool): 선두 태스크 마감 임박(<=10s) 여부.
        cost_level (int): 고비용 국면 여부(1=총 시간당요금>$9, 0=그 외).
        scale_success (bool): 증설 성공 여부.

    Returns:
        float: SCALE 보상.
    """
    if not scale_success:
        # 물리적 자원 부족 또는 OutOfCapacity 가동 실패 → 강한 페널티
        return -10.0

    if action == 4:  # Spot-A 증설 (성능 지향)
        reward = (4.0 if urgent else -1.5) - 3.5
        reward += -3.0 if cost_level == 1 else 3.0
        return reward
    elif action == 5:  # Spot-B 증설 (저가 안정)
        reward = (1.0 if urgent else 0.0) - 2.0
        reward += -2.0 if cost_level == 1 else 1.0
        return reward

    # 정의되지 않은 액션 — 방어적 폴백(정상 경로에서는 도달하지 않음)
    return 0.0


def impossible_assign_reward():
    """
    가용 워커가 없는데 ASSIGN을 시도한 '불가능 배정'의 보상.

    실제 스케줄러는 이 경우 아무 Q-업데이트 없이 태스크를 defer할 뿐이며, 액션 마스킹 때문에
    거의 도달하지 않는다. 시뮬레이터가 이때 인위적 음(-) 보상을 주면 실제에 없는 신호를 학습하므로
    0.0(중립)으로 맞춘다.

    Returns:
        float: 0.0
    """
    return 0.0
