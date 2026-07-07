# ==============================================================================
# WE-MEET: 정적 규칙 기반 스케줄링 모듈 (head/scheduler_static.py)
# ==============================================================================

import time
import threading
import head.state as gcs_state
import head.cluster_manager as cluster_manager
import head.dashboard.server as dashboard

def run_static_scheduler_step(MAX_SPOT_SCALE, scale_in_timer, run_task_on_worker, get_next_runnable_task, get_current_spot_scale, run_scale_decisions=False):
    """
    Static 스케줄러의 1주기 의사결정 및 연산 할당 작업을 수행합니다.
    - FIFO 기반 작업 배정
    - 대기열 크기 비례 정적 오토스케일링
    """
    spot_scale = get_current_spot_scale()
    
    # 1. 정적 규칙 오토스케일링 (지정된 스케일 결정 주기에만 실행)
    if run_scale_decisions:
        with gcs_state.queue_lock:
            q_len_real = len(gcs_state.task_queue)
        
        if q_len_real >= 8 and spot_scale < MAX_SPOT_SCALE - 1:
            dashboard.log_event(f"[Static Scale-Out] 대기 큐 심각 적체 ({q_len_real} >= 8) -> Spot 워커 2대 동시 증설 지시")
            if cluster_manager.scale_out_worker("spot_a"):
                spot_scale += 1
            if cluster_manager.scale_out_worker("spot_a"):
                spot_scale += 1
        elif q_len_real >= 4 and spot_scale < MAX_SPOT_SCALE:
            dashboard.log_event(f"[Static Scale-Out] 대기 큐 크기 임계치 초과 ({q_len_real} >= 4) -> Spot 워커 1대 증설 지시")
            if cluster_manager.scale_out_worker("spot_a"):
                spot_scale += 1
                
        if q_len_real == 0:
            scale_in_timer += 1.0
            if scale_in_timer >= 3.0 and spot_scale > 0:
                dashboard.log_event("[Static Scale-In] 대기열 유휴 상태 3초 지속 -> Spot 워커 순차 회수")
                if cluster_manager.scale_in_specific_worker("spot_a"):
                    spot_scale -= 1
                    scale_in_timer = 0.0
        else:
            scale_in_timer = 0.0
        
    # 2. FIFO 및 순차 태스크 할당
    while True:
        target_task = get_next_runnable_task()
        if not target_task:
            break
            
        assigned = False
        with gcs_state.registry_lock:
            # On-Demand 노드 우선 탐색 후 Spot 할당 (메모리 90% 이상 과부하 예방)
            for wid, info in sorted(gcs_state.worker_registry.items(), key=lambda x: 0 if x[1]["node_type"] == "on_demand" else 1):
                if info["status"] == "IDLE" and info.get("mem", 0.0) < 90.0:
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
