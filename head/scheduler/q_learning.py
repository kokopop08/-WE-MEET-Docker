# ==============================================================================
# WE-MEET: Q-Learning 기반 지능형 비용/SLA 최적화 스케줄링 모듈 (head/scheduler_qlearning.py)
# ==============================================================================

import time
import threading
import head.state as gcs_state
import head.cluster_manager as cluster_manager
import head.dashboard.server as dashboard
import head.q_learning.state_features as state_features


def _calculate_next_state():
    """다음 상태(next_state)를 단일 상태 함수로 위임하여 산출한다 (인코딩 드리프트 방지)."""
    state, _ = state_features.compute_current_state(gcs_state)
    return state

def run_qlearning_scheduler_step(MAX_SPOT_SCALE, empty_queue_duration, agent, run_task_on_worker, get_next_runnable_task, get_current_spot_scale, run_scale_decisions=False):
    """
    Q-Learning 스케줄러 of 1주기 의사결정 및 연산 할당 작업을 수행합니다.
    - 4차원 상태 공간 (w_mix, a_mix, u_sla, b_avail) 기반 6대 행동 스케줄링
    - 예산 고갈 시 Action Masking 안전 가드
    """
    if not gcs_state.Q_LEARNING_TRAINING_MODE:
        agent.epsilon = 0.0

    spot_scale = get_current_spot_scale()
    
    if run_scale_decisions:
        with gcs_state.registry_lock:
            idle_spot_a = sum(1 for info in gcs_state.worker_registry.values() if info["node_type"] == "spot_a" and info["status"] == "IDLE")
            idle_spot_b = sum(1 for info in gcs_state.worker_registry.values() if info["node_type"] == "spot_b" and info["status"] == "IDLE")
            
        if idle_spot_a > 0 or idle_spot_b > 0:
            empty_queue_duration += 1.0
        else:
            empty_queue_duration = 0.0
            
        if empty_queue_duration >= 3.0 and spot_scale > 0:
            with gcs_state.registry_lock:
                has_spot_a = any(info["node_type"] == "spot_a" for info in gcs_state.worker_registry.values())
                has_spot_b = any(info["node_type"] == "spot_b" for info in gcs_state.worker_registry.values())
            
            if has_spot_a:
                if cluster_manager.scale_in_specific_worker("spot_a"):
                    dashboard.log_event("[Q-Learning Scale-In] 무부하 3초 유지로 인한 Spot-A 노드 안전 회수")
                    spot_scale -= 1
                    empty_queue_duration = 0.0
            elif has_spot_b:
                if cluster_manager.scale_in_specific_worker("spot_b"):
                    dashboard.log_event("[Q-Learning Scale-In] 무부하 3초 유지로 인한 Spot-B 노드 안전 회수")
                    spot_scale -= 1
                    empty_queue_duration = 0.0

    deferred_tasks = []
    while True:
        with gcs_state.queue_lock:
            q_len_real = len(gcs_state.task_queue)
        if q_len_real == 0:
            break
            
        # 상태 특징 산출을 단일 함수로 위임 (스케줄러/완료피드백/시뮬레이터 인코딩 통일)
        state, ctx = state_features.compute_current_state(gcs_state)
        w1_idle = 1 if ctx["idle_od"] else 0
        w2_idle = 1 if ctx["idle_spot_a"] else 0
        w3_idle = 1 if ctx["idle_spot_b"] else 0
        peek_task = ctx["peek_task"]
        urgent = ctx["sla_bucket"] >= 2      # 마감 임박(<=10s) 여부
        u_sla = 1 if urgent else 0           # HOLD/SCALE 결정시점 보상 휴리스틱 호환용
        c_level = ctx["cost_level"]          # 고비용 국면(총요금>$9) 호환용

        available_actions = [3]
        
        if peek_task:
            if w1_idle > 0:
                available_actions.append(0)
            if w2_idle > 0:
                available_actions.append(1)
            if w3_idle > 0:
                available_actions.append(2)
                    
        # Cold start 지연 마스킹: 현재 기동 중(LAUNCHING)이지만 GCS에 미등록 상태인 스팟 노드가 있는 경우 추가 증설 일시 차단
        has_launching = False
        if gcs_state.DOCKER_CLIENT is not None:
            try:
                containers = gcs_state.DOCKER_CLIENT.containers.list(all=True)
                with gcs_state.registry_lock:
                    registered_ids = list(gcs_state.worker_registry.keys())
                for c in containers:
                    c_name = c.name
                    if c_name.startswith("babyray-worker-2-") or c_name.startswith("babyray-worker-3-"):
                        if c.status in ["running", "created"]:
                            wid = c_name.replace("babyray-", "")
                            if wid not in registered_ids:
                                has_launching = True
                                break
            except Exception:
                pass

        if run_scale_decisions and spot_scale < MAX_SPOT_SCALE and not has_launching:
            available_actions.append(4)
            available_actions.append(5)

        if gcs_state.virtual_budget <= 0.0:
            if 0 in available_actions:
                available_actions.remove(0)
            if 4 in available_actions:
                available_actions.remove(4)
            if 5 in available_actions:
                available_actions.remove(5)

        # 호스트 물리 자원 부족 시 증설 액션 마스킹
        from head.resource_guard import is_host_resource_sufficient
        if not is_host_resource_sufficient():
            if 4 in available_actions:
                available_actions.remove(4)
            if 5 in available_actions:
                available_actions.remove(5)

        if u_sla == 1 and any(act in available_actions for act in [0, 1, 2]):
            if 3 in available_actions:
                available_actions.remove(3)

        if available_actions == [3]:
            break

        action = agent.choose_action(state, available_actions)

        if action in [0, 1, 2]:
            target_type = ["on_demand", "spot_a", "spot_b"][action]
            target_task = get_next_runnable_task()
            
            if not target_task:
                break
                
            worker_id = None
            worker_info = None
            with gcs_state.registry_lock:
                for wid, info in gcs_state.worker_registry.items():
                    if info["node_type"] == target_type and info["status"] == "IDLE":
                        worker_id = wid
                        worker_info = info.copy()
                        gcs_state.worker_registry[wid]["status"] = "BUSY"
                        break
            
            if worker_info:
                # ASSIGN 액션의 학습은 '배정 시점의 낙관적 보상'이 아니라, 태스크가 실제로 완료되거나
                # 회수(Eviction)/OOM으로 실패했을 때 run_task_on_worker의 완료 피드백에서 수행한다.
                # (state, action)을 실행 스레드로 전달해야 지연 보상 경로가 활성화되어 회수 페널티가 Q값에 반영된다.
                threading.Thread(
                    target=run_task_on_worker,
                    args=(worker_id, worker_info, target_task, state, action),
                    daemon=True
                ).start()
            else:
                deferred_tasks.append(target_task)
            
        elif action == 3:
            dashboard.log_event(f"[Q-Learning Action] HOLD 상태 선택 (대기열 크기: {q_len_real})")
            if gcs_state.Q_LEARNING_TRAINING_MODE:
                hold_penalty = 0.0
                with gcs_state.queue_lock:
                    for t in gcs_state.task_queue:
                        time_over = time.time() - t["deadline"]
                        if time_over > 0.0:
                            hold_penalty += time_over * agent.DELAY_PENALTY_WEIGHT * 0.2
                
                reward = 1.0 - hold_penalty
                next_state = _calculate_next_state()
                agent.update_q_value(state, action, reward, next_state)
                agent.save_q_table()
                from head.scheduler.task_executor import log_online_training
                log_online_training(state, action, reward, next_state, agent.epsilon)
                dashboard.log_event(f"[Q-Learning Update] State={state} | Action={action} (HOLD) | Reward={reward:.4f} | NextState={next_state} | Epsilon={agent.epsilon:.4f}")
            break
            
        elif action == 4:
            dashboard.log_event(f"[Q-Learning Action] SCALE_OUT_SPOT_A 트리거 -> Spot-A 노드 추가 증설")
            scale_success = False
            if q_len_real >= 6 and spot_scale < MAX_SPOT_SCALE - 1:
                dashboard.log_event(f"[Q-Learning Scale-Out] 대기 큐 심각 적체({q_len_real}개) -> Spot-A 2대 동시 증설")
                s1 = cluster_manager.scale_out_worker("spot_a")
                if s1:
                    spot_scale += 1
                    scale_success = True
                s2 = cluster_manager.scale_out_worker("spot_a")
                if s2:
                    spot_scale += 1
                    scale_success = True
            else:
                if cluster_manager.scale_out_worker("spot_a"):
                    spot_scale += 1
                    scale_success = True
                    
            if gcs_state.Q_LEARNING_TRAINING_MODE:
                if scale_success:
                    reward = (4.0 if u_sla == 1 else -1.5) - 3.5
                    if c_level == 1:
                        reward -= 3.0
                    else:
                        reward += 3.0
                else:
                    # 물리적 자원 부족 또는 OutOfCapacity 가동 실패 시 강력한 페널티 벌점 부과
                    reward = -10.0
                    
                next_state = _calculate_next_state()
                agent.update_q_value(state, action, reward, next_state)
                agent.save_q_table()
                from head.scheduler.task_executor import log_online_training
                log_online_training(state, action, reward, next_state, agent.epsilon)
                dashboard.log_event(f"[Q-Learning Update] State={state} | Action={action} (SCALE_SPOT_A) | Reward={reward:.4f} | NextState={next_state} | Epsilon={agent.epsilon:.4f}")
            break
            
        elif action == 5:
            dashboard.log_event(f"[Q-Learning Action] SCALE_OUT_SPOT_B 트리거 -> Spot-B 노드 추가 증설")
            scale_success = False
            if q_len_real >= 6 and spot_scale < MAX_SPOT_SCALE - 1:
                dashboard.log_event(f"[Q-Learning Scale-Out] 대기 큐 심각 적체({q_len_real}개) -> Spot-B 2대 동시 증설")
                s1 = cluster_manager.scale_out_worker("spot_b")
                if s1:
                    spot_scale += 1
                    scale_success = True
                s2 = cluster_manager.scale_out_worker("spot_b")
                if s2:
                    spot_scale += 1
                    scale_success = True
            else:
                if cluster_manager.scale_out_worker("spot_b"):
                    spot_scale += 1
                    scale_success = True
                    
            if gcs_state.Q_LEARNING_TRAINING_MODE:
                if scale_success:
                    reward = (1.0 if u_sla == 1 else 0.0) - 2.0
                    if c_level == 1:
                        reward -= 2.0
                    else:
                        reward += 1.0
                else:
                    # 물리적 자원 부족 또는 OutOfCapacity 가동 실패 시 강력한 페널티 벌점 부과
                    reward = -10.0
                    
                next_state = _calculate_next_state()
                agent.update_q_value(state, action, reward, next_state)
                agent.save_q_table()
                from head.scheduler.task_executor import log_online_training
                log_online_training(state, action, reward, next_state, agent.epsilon)
                dashboard.log_event(f"[Q-Learning Update] State={state} | Action={action} (SCALE_SPOT_B) | Reward={reward:.4f} | NextState={next_state} | Epsilon={agent.epsilon:.4f}")
            break
            
    if deferred_tasks:
        with gcs_state.queue_lock:
            for task in reversed(deferred_tasks):
                gcs_state.task_queue.insert(0, task)
                
    return empty_queue_duration
