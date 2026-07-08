# ==============================================================================
# WE-MEET: 동적 부하 인지형 스케줄링 모듈 (head/scheduler/dynamic.py)
#
# [철학] 인간이 짤 수 있는 최고 수준의 Task↔Node 매칭 하드코딩.
#   - LSTM(메모리 폭식) -> On-Demand/Spot-A (작은 노드 OOM 회피, Spot-B 금지)
#   - CNN(무거운 연산)  -> Spot-A 우선 (저속 노드의 회수 룰렛 복리 노출 회피)
#   - RNN(초경량)       -> Spot-B 우선 (단독 ~5초라 10초 룰렛 회피 + 극가성비)
#   증설 노드 타입도 큐의 모형 구성에 맞춰 가변 선택한다.
# ==============================================================================

import time
import threading
import head.state as gcs_state
import head.cluster_manager as cluster_manager
import head.dashboard.server as dashboard

# 모델별 선호 노드 타입 순서 (앞쪽일수록 우선). LSTM은 Spot-B를 아예 후보에서 제외(OOM·저속 회피).
MODEL_NODE_PREFERENCE = {
    "LSTM": ["on_demand", "spot_a"],
    "CNN":  ["spot_a", "on_demand", "spot_b"],
    "RNN":  ["spot_b", "spot_a", "on_demand"],
    "MERGE": ["on_demand", "spot_a", "spot_b"],
}

def run_dynamic_scheduler_step(MAX_SPOT_SCALE, scale_in_timer, run_task_on_worker, get_next_runnable_task, get_current_spot_scale, run_scale_decisions=False):
    """
    Dynamic 스케줄러의 1주기 의사결정 및 연산 할당 작업을 수행합니다.
    - Task-Aware 노드 매칭(모델 성격 -> 선호 노드 타입)
    - 부하/모형 적응형 이종 증설
    - 자원 임계 경합 방지(간섭 회피) 및 백필링
    """
    spot_scale = get_current_spot_scale()

    # 1. 부하/모형 적응형 스케일아웃 정책 (지정된 스케일 결정 주기에만 실행)
    if run_scale_decisions:
        with gcs_state.registry_lock:
            active_workers = list(gcs_state.worker_registry.values())

        with gcs_state.queue_lock:
            q_len_real = len(gcs_state.task_queue)
            has_lstm = any(t.get("model_type") == "LSTM" for t in gcs_state.task_queue)
            has_cnn = any(t.get("model_type") == "CNN" for t in gcs_state.task_queue)

        if active_workers:
            avg_cpu = sum(info.get("cpu", 0.0) for info in active_workers) / len(active_workers)
            avg_mem = sum(info.get("mem", 0.0) for info in active_workers) / len(active_workers)
        else:
            avg_cpu, avg_mem = 0.0, 0.0

        # 증설 타입 결정: 무거운 모형(LSTM/CNN)이 큐에 있으면 빠른 Spot-A, RNN 위주면 저가 Spot-B.
        target_type = "spot_a" if (has_lstm or has_cnn) else "spot_b"

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

    # 2. Task-Aware 매칭 배정 (모델 선호 노드 타입 + 간섭 회피 + 백필링)
    deferred_tasks = []
    while True:
        target_task = get_next_runnable_task()
        if not target_task:
            break

        model = str(target_task.get("model_type", "CNN")).upper()
        pref = MODEL_NODE_PREFERENCE.get(model, ["spot_a", "on_demand", "spot_b"])

        selected_worker_id = None
        selected_worker_info = None

        with gcs_state.registry_lock:
            candidates = []
            for wid, info in gcs_state.worker_registry.items():
                if info["status"] != "IDLE":
                    continue
                cpu_val = info.get("cpu", 0.0)
                mem_val = info.get("mem", 0.0)
                # 간섭 회피: 임계 과부하 노드는 후보에서 배제
                if cpu_val >= 80.0 or mem_val >= 75.0:
                    continue
                ntype = info["node_type"]
                if ntype not in pref:
                    # 선호 타입이 아니면 배제 (예: LSTM에 대한 Spot-B는 후보에서 제외되어 OOM 회피)
                    continue
                # (선호순위, least-loaded) 순으로 정렬하기 위한 키
                candidates.append((pref.index(ntype), cpu_val * 0.5 + mem_val * 0.5, wid, info))

            if candidates:
                candidates.sort(key=lambda x: (x[0], x[1]))
                _, _, selected_worker_id, selected_worker_info = candidates[0]
                gcs_state.worker_registry[selected_worker_id]["status"] = "BUSY"

        if selected_worker_info:
            threading.Thread(
                target=run_task_on_worker,
                args=(selected_worker_id, selected_worker_info.copy(), target_task, None, None),
                daemon=True
            ).start()
        else:
            # 선호 노드가 포화/부재 시 스팸 방지 로그 후 백필링(보류)
            cur_time = time.time()
            last_log = getattr(run_dynamic_scheduler_step, "_last_log_time", 0.0)
            if cur_time - last_log >= 5.0:
                dashboard.log_event(f"[Dynamic Staggered] Task-Aware 매칭 보류: {target_task['task_id']}({model}) 선호 노드 미가용으로 지연 (대기 중)")
                run_dynamic_scheduler_step._last_log_time = cur_time
            deferred_tasks.append(target_task)

    # 보류된 태스크들의 순서를 유지하여 대기열 선두로 복원
    if deferred_tasks:
        with gcs_state.queue_lock:
            for task in reversed(deferred_tasks):
                gcs_state.task_queue.insert(0, task)

    return scale_in_timer
