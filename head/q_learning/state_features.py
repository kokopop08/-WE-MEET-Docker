# ==============================================================================
# WE-MEET: Q-Learning 상태 특징(State Feature) 단일 산출 모듈
# (head/q_learning/state_features.py)
#
# [존재 이유]
# 상태 계산 로직이 여러 곳(스케줄러 현재상태/다음상태, 완료 피드백, 오프라인 시뮬레이터)에
# 복붙되어 있어, 차원을 늘리면 서로 어긋나 Bellman 업데이트가 깨지는 문제가 있었다.
# 모든 경로가 이 모듈의 함수를 공유하여 상태 인코딩의 유일 진실을 보장한다.
#
# [상태 튜플 (6-tuple)]
#   (q_bucket, head_model, a_mix, sla_bucket, danger_phase, budget_level)
#     - q_bucket     : 대기열 적체 깊이 (0 빈 / 1 경1-3 / 2 중4-7 / 3 과8+)  → 캐스케이드 신호
#     - head_model   : 선두 실행가능 태스크 성격 (0 연산바운드 CNN / 1 메모리바운드 RNN·LSTM)
#     - a_mix        : IDLE 노드 비트맵 (OD=1, Spot-A=2, Spot-B=4)
#     - sla_bucket   : 마감 완급 (0 여유>30s / 1 중10-30s / 2 임박<=10s)
#     - danger_phase : 스팟 회수 위험구간 여부 (0/1) → 룰렛 타이밍 신호
#     - budget_level : 잔여 예산 수준 (0 위험 / 1 낮음 / 2 여유)
# ==============================================================================

import time
from common.failure_simulator import FailureSimulator

# --- 버킷 임계 상수 (튜닝 포인트) ---
Q_LIGHT_MAX = 3        # 1~3 : 경적체
Q_MED_MAX = 7          # 4~7 : 중적체 (8+ : 과적체)
SLA_TIGHT_SEC = 10.0   # 이하: 임박
SLA_MED_SEC = 30.0     # 이하: 중간 여유
BUDGET_CRITICAL = 0.7  # 미만: 예산 위험
BUDGET_LOW = 3.0       # 미만: 예산 낮음
MEM_BOUND_MODELS = ("RNN", "LSTM")


def _q_bucket(q_len):
    if q_len <= 0:
        return 0
    if q_len <= Q_LIGHT_MAX:
        return 1
    if q_len <= Q_MED_MAX:
        return 2
    return 3


def _head_model_class(model_type):
    """선두 태스크 성격: 메모리 바운드(RNN/LSTM)=1, 그 외(CNN/MERGE/None)=0."""
    if model_type and str(model_type).upper() in MEM_BOUND_MODELS:
        return 1
    return 0


def _sla_bucket(time_left):
    if time_left is None:
        return 0
    if time_left <= SLA_TIGHT_SEC:
        return 2
    if time_left <= SLA_MED_SEC:
        return 1
    return 0


def _budget_level(budget):
    if budget < BUDGET_CRITICAL:
        return 0
    if budget < BUDGET_LOW:
        return 1
    return 2


def compute_state(q_len, head_model, head_time_left,
                  idle_od, idle_spot_a, idle_spot_b, budget, danger_phase):
    """
    환경-비의존 순수 함수: 이미 추출된 원시값으로 6-튜플 상태를 산출한다.
    실제 docker 경로와 오프라인 시뮬레이터가 동일하게 이 함수를 호출해 인코딩을 공유한다.
    """
    a_mix = (1 if idle_od else 0) + (2 if idle_spot_a else 0) + (4 if idle_spot_b else 0)
    return (
        _q_bucket(q_len),
        _head_model_class(head_model),
        a_mix,
        _sla_bucket(head_time_left),
        1 if danger_phase else 0,
        _budget_level(budget),
    )


def _peek_runnable(gcs_state):
    """대기열에서 의존성이 충족된 선두 태스크를 (pop 없이) 반환. 없으면 None."""
    for t in gcs_state.task_queue:
        deps_met = True
        for dep in t.get("dependencies", []):
            if not gcs_state.completed_tasks_cache.get(dep, False):
                deps_met = False
                break
        if deps_met:
            return t
    return None


def compute_current_state(gcs_state, now=None):
    """
    실제 docker 경로(gcs_state 인메모리 스토어 기반) 상태 산출.

    스케줄러 현재상태/다음상태, 완료 피드백 next_state가 모두 이 함수를 호출한다.

    Returns:
        tuple(state, ctx):
            state (tuple): 6-튜플 상태.
            ctx (dict): 행동 마스킹 등에 필요한 부가 관측치
                        (idle_od, idle_spot_a, idle_spot_b, peek_task, head_model,
                         head_time_left, danger_phase, sla_bucket, budget_level, cost_level, q_len).
    """
    now = now if now is not None else time.time()

    with gcs_state.queue_lock:
        q_len = len(gcs_state.task_queue)
        peek_task = _peek_runnable(gcs_state)
        head_model = peek_task.get("model_type") if peek_task else None
        head_time_left = (peek_task["deadline"] - now) if peek_task else None

    with gcs_state.registry_lock:
        idle_od = any(i["node_type"] == "on_demand" and i["status"] == "IDLE"
                      for i in gcs_state.worker_registry.values())
        idle_a = any(i["node_type"] == "spot_a" and i["status"] == "IDLE"
                     for i in gcs_state.worker_registry.values())
        idle_b = any(i["node_type"] == "spot_b" and i["status"] == "IDLE"
                     for i in gcs_state.worker_registry.values())
        # 총 시간당 요금 (기존 c_level 호환용: >$9면 고비용 국면)
        total_cost_per_hour = 0.0
        for info in gcs_state.worker_registry.values():
            ntype = info.get("node_type", "on_demand").lower()
            if ntype == "on_demand":
                total_cost_per_hour += 7.10
            elif ntype == "spot_a":
                total_cost_per_hour += 2.20
            elif ntype == "spot_b":
                total_cost_per_hour += 0.90

    danger = FailureSimulator.current_danger_phase(now)
    budget = getattr(gcs_state, "virtual_budget", 0.0)

    state = compute_state(q_len, head_model, head_time_left, idle_od, idle_a, idle_b, budget, danger)
    ctx = {
        "q_len": q_len,
        "peek_task": peek_task,
        "head_model": head_model,
        "head_time_left": head_time_left,
        "idle_od": idle_od,
        "idle_spot_a": idle_a,
        "idle_spot_b": idle_b,
        "danger_phase": danger,
        "sla_bucket": state[3],
        "budget_level": state[5],
        "cost_level": 1 if total_cost_per_hour > 9.00 else 0,
    }
    return state, ctx
