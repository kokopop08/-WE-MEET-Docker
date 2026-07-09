"""WE-MEET: 정적 규칙 기반 스케줄링 모듈 (head/scheduler/static.py)

[철학] "속도 최우선 / 비용·자원 무시" 극단 베이스라인.
  - On-Demand를 최우선으로 즉시 사용하고, 큐가 밀리면 빠른 Spot-A를 공격적으로 최대치까지 증설.
  - 저속 Spot-B는 완전히 배제. 자원(메모리/부하) 상태나 예산은 전혀 보지 않는다(무방비).
  - 기대 거동: SLA/Throughput은 최상위지만, 값비싼 자원을 남발해 예산이 조기에 파산한다.
"""

import time
import threading
import wemeet.cluster.gcs_state as gcs_state
import wemeet.cluster.manager as cluster_manager
from wemeet.observability import event_log as _obslog

def run_static_scheduler_step(MAX_SPOT_SCALE, scale_in_timer, run_task_on_worker, get_next_runnable_task, get_current_spot_scale, run_scale_decisions=False):
    """
    Static 스케줄러의 1주기 의사결정 및 연산 할당 작업을 수행합니다.
    - FIFO 기반, On-Demand 최우선 순차 배정 (Spot-B 배제)
    - 큐 적체 시 빠른 Spot-A를 공격적으로 최대치까지 증설 (비용 무시)
    """
    spot_scale = get_current_spot_scale()

    # 1. 공격적 정적 오토스케일링 (지정된 스케일 결정 주기에만 실행)
    if run_scale_decisions:
        with gcs_state.queue_lock:
            q_len_real = len(gcs_state.task_queue)

        # 큐가 조금이라도 밀리면 빠른 Spot-A를 최대치까지 밀어붙인다 (비용 고려 없음)
        if q_len_real >= 6 and spot_scale < MAX_SPOT_SCALE - 1:
            _obslog.log_event(f"[Static Scale-Out] 대기 큐 심각 적체 ({q_len_real} >= 6) -> Spot-A 워커 2대 동시 증설 지시")
            if cluster_manager.scale_out_worker("spot_a"):
                spot_scale += 1
            if cluster_manager.scale_out_worker("spot_a"):
                spot_scale += 1
        elif q_len_real >= 2 and spot_scale < MAX_SPOT_SCALE:
            _obslog.log_event(f"[Static Scale-Out] 대기 큐 적체 ({q_len_real} >= 2) -> Spot-A 워커 1대 증설 지시")
            if cluster_manager.scale_out_worker("spot_a"):
                spot_scale += 1

        if q_len_real == 0:
            scale_in_timer += 1.0
            if scale_in_timer >= 3.0 and spot_scale > 0:
                _obslog.log_event("[Static Scale-In] 대기열 유휴 상태 3초 지속 -> Spot-A 워커 순차 회수")
                if cluster_manager.scale_in_specific_worker("spot_a"):
                    spot_scale -= 1
                    scale_in_timer = 0.0
        else:
            scale_in_timer = 0.0

    # 2. FIFO 및 순차 태스크 할당 (On-Demand 최우선 -> Spot-A. 자원 상태는 보지 않는다=무방비)
    while True:
        target_task = get_next_runnable_task()
        if not target_task:
            break

        assigned = False
        with gcs_state.registry_lock:
            # On-Demand 우선 탐색 후 Spot-A 할당. Spot-B는 배정 대상에서 제외한다.
            for wid, info in sorted(gcs_state.worker_registry.items(), key=lambda x: 0 if x[1]["node_type"] == "on_demand" else 1):
                if info["status"] == "IDLE" and info["node_type"] in ("on_demand", "spot_a"):
                    gcs_state.worker_registry[wid]["status"] = "BUSY"
                    threading.Thread(
                        target=run_task_on_worker,
                        args=(wid, info.copy(), target_task, None, None),
                        daemon=True
                    ).start()
                    assigned = True
                    break

        if not assigned:
            with gcs_state.queue_lock:
                gcs_state.task_queue.insert(0, target_task)
            break

    return scale_in_timer
