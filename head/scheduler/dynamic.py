# ==============================================================================
# WE-MEET: 동적 부하 인지형 스케줄링 모듈 (head/scheduler_dynamic.py)
# ==============================================================================

import time
import threading
import head.state as gcs_state
import head.cluster_manager as cluster_manager
import head.dashboard.server as dashboard

def run_dynamic_scheduler_step(MAX_SPOT_SCALE, scale_in_timer, run_task_on_worker, get_next_runnable_task, get_current_spot_scale, run_scale_decisions=False):
    """
    Dynamic 스케줄러의 1주기 의사결정 및 연산 할당 작업을 수행합니다.
    - 실시간 부하 모니터링 기반 오토스케일링
    - 최소 부하 우선 분산 배정 (Spread)
    - 자원 임계 경합 방지 지연 배정 (Staggered)
    """
    spot_scale = get_current_spot_scale()
    
    # 1. 평균 부하 또는 큐 대기 적체 기반 스케일아웃 정책 (지정된 스케일 결정 주기에만 실행)
    if run_scale_decisions:
        with gcs_state.registry_lock:
            active_workers = list(gcs_state.worker_registry.values())
            
        with gcs_state.queue_lock:
            q_len_real = len(gcs_state.task_queue)
        
        if active_workers:
            avg_cpu = sum(info.get("cpu", 0.0) for info in active_workers) / len(active_workers)
            avg_mem = sum(info.get("mem", 0.0) for info in active_workers) / len(active_workers)
        else:
            avg_cpu, avg_mem = 0.0, 0.0
            
        # 대기 큐 내부 태스크 중 메모리 집약형 모형인 LSTM 탑재 여부 확인
        has_lstm = False
        with gcs_state.queue_lock:
            has_lstm = any(t.get("model_type") == "LSTM" for t in gcs_state.task_queue)
            
        # 평균 리소스 부하가 심각하거나 LSTM 모형이 포함된 경우 고성능 Spot-A를, 그렇지 않으면 Spot-B를 동적 선택
        target_type = "spot_a" if (avg_cpu > 75.0 or avg_mem > 70.0 or has_lstm) else "spot_b"
            
        # 대기 큐에 작업이 3개 이상 밀렸거나 평균 리소스 부하가 높을 시 스케일아웃
        if q_len_real >= 8 and spot_scale < MAX_SPOT_SCALE - 1:
            dashboard.log_event(f"[Dynamic Scale-Out] 대기 큐 심각 적체({q_len_real}개) -> Spot-{target_type[-1].upper()} 노드 2대 동시 증설")
            if cluster_manager.scale_out_worker(target_type):
                spot_scale += 1
            if cluster_manager.scale_out_worker(target_type):
                spot_scale += 1
        elif ((avg_cpu > 70.0 or avg_mem > 70.0) or q_len_real >= 3) and spot_scale < MAX_SPOT_SCALE:
            dashboard.log_event(f"[Dynamic Scale-Out] 대기 큐 적체({q_len_real}개) 또는 고부하 감지 -> Spot-{target_type[-1].upper()} 노드 1대 증설")
            if cluster_manager.scale_out_worker(target_type):
                spot_scale += 1
                
        if q_len_real == 0 and avg_cpu < 20.0 and avg_mem < 20.0:
            scale_in_timer += 1.0
            if scale_in_timer >= 3.0 and spot_scale > 0:
                # 요금이 더 비싼 spot_a를 우선 회수하여 예산 효율을 최적화
                with gcs_state.registry_lock:
                    has_spot_a = any(info.get("node_type") == "spot_a" for info in gcs_state.worker_registry.values())
                    
                reclaim_type = "spot_a" if has_spot_a else "spot_b"
                dashboard.log_event(f"[Dynamic Scale-In] 저부하 유휴 상태 3초 유지 -> Spot-{reclaim_type[-1].upper()} 워커 회수")
                if cluster_manager.scale_in_specific_worker(reclaim_type):
                    spot_scale -= 1
                    scale_in_timer = 0.0
        else:
            scale_in_timer = 0.0
        
    # 2. 리소스 인지형 간섭 회피 분산 배정 (Spread & Staggered & Backfilling)
    deferred_tasks = []
    while True:
        target_task = get_next_runnable_task()
        if not target_task:
            break
            
        assigned = False
        selected_worker_id = None
        selected_worker_info = None
        
        with gcs_state.registry_lock:
            candidate_workers = []
            for wid, info in gcs_state.worker_registry.items():
                if info["status"] == "IDLE":
                    cpu_val = info.get("cpu", 0.0)
                    mem_val = info.get("mem", 0.0)
                    
                    # 간섭 회피: CPU가 80%를 넘거나 Memory가 75%를 넘은 임계 과부하 상태 노드는 할당 원천 배제
                    if cpu_val >= 80.0 or mem_val >= 75.0:
                        continue
                    candidate_workers.append((wid, info, cpu_val * 0.5 + mem_val * 0.5))
                    
            if candidate_workers:
                # Least-Loaded 정렬
                candidate_workers.sort(key=lambda x: x[2])
                selected_worker_id = candidate_workers[0][0]
                selected_worker_info = candidate_workers[0][1]
                gcs_state.worker_registry[selected_worker_id]["status"] = "BUSY"
                
        if selected_worker_info:
            threading.Thread(
                target=run_task_on_worker,
                args=(selected_worker_id, selected_worker_info.copy(), target_task, None, None),
                daemon=True
            ).start()
            assigned = True
        else:
            # 5초에 한 번씩만 스팸 방지용으로 로그 기록
            cur_time = time.time()
            last_log = getattr(run_dynamic_scheduler_step, "_last_log_time", 0.0)
            if cur_time - last_log >= 5.0:
                dashboard.log_event(f"[Dynamic Staggered] 간섭 회피: 모든 가용 노드 자원 포화로 {target_task['task_id']} 할당 보류 및 지연 (대기 중)")
                run_dynamic_scheduler_step._last_log_time = cur_time
            
        if not assigned:
            # 백필링 적용: 현재 배정할 수 없는 태스크는 보류 리스트에 넣고 큐 후순위 탐색 계속 수행
            deferred_tasks.append(target_task)
            
    # 보류된 태스크들의 순서를 유지하여 대기열 선두로 복원 복구
    if deferred_tasks:
        with gcs_state.queue_lock:
            for task in reversed(deferred_tasks):
                gcs_state.task_queue.insert(0, task)
                
    return scale_in_timer
