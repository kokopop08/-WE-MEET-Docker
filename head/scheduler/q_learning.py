# ==============================================================================
# WE-MEET: Q-Learning 기반 지능형 비용/SLA 최적화 스케줄링 모듈 (head/scheduler_qlearning.py)
# ==============================================================================

import time
import threading
import head.state as gcs_state
import head.cluster_manager as cluster_manager
import head.dashboard.server as dashboard

def run_qlearning_scheduler_step(MAX_SPOT_SCALE, empty_queue_duration, agent, run_task_on_worker, get_next_runnable_task, get_current_spot_scale, run_scale_decisions=False):
    """
    Q-Learning 스케줄러 of 1주기 의사결정 및 연산 할당 작업을 수행합니다.
    - 4차원 상태 공간 (w_mix, a_mix, u_sla, b_avail) 기반 6대 행동 스케줄링
    - 예산 고갈 시 Action Masking 안전 가드
    """
    # 훈련 모드 여부에 맞게 에이전트 탐험율(Epsilon)을 실시간 동기화 제어
    if not gcs_state.Q_LEARNING_TRAINING_MODE:
        agent.epsilon = 0.0  # 추론형 모드: 완전히 기존 Q-Table 지식만을 근거로 판단 (탐험 배제)
    # 훈련 모드에서는 update_q_value()의 decay_rate(0.995)에 의해 epsilon_min(0.05)까지 자연 감쇄

    spot_scale = get_current_spot_scale()
    
    # 1. 룰 기반 Scale-In 작동 보완 (지정된 스케일 결정 주기에만 실행)
    if run_scale_decisions:
        with gcs_state.registry_lock:
            idle_spot_a = sum(1 for info in gcs_state.worker_registry.values() if info["node_type"] == "spot_a" and info["status"] == "IDLE")
            idle_spot_b = sum(1 for info in gcs_state.worker_registry.values() if info["node_type"] == "spot_b" and info["status"] == "IDLE")
            
        if idle_spot_a > 0 or idle_spot_b > 0:
            empty_queue_duration += 1.0
        else:
            empty_queue_duration = 0.0
            
        if empty_queue_duration >= 3.0 and spot_scale > 0:
            # 비용 효율을 위해 가동 비용이 비싼 spot_a를 우선 회수하고 없으면 spot_b를 회수합니다.
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

    # 2. Q-Learning 의사결정 루프 (Backfilling 적용)
    deferred_tasks = []
    while True:
        with gcs_state.queue_lock:
            q_len_real = len(gcs_state.task_queue)
        if q_len_real == 0:
            break
            
        # 4차원 상태 공간 리팩토링 산출
        with gcs_state.queue_lock:
            cnn_count = sum(1 for t in gcs_state.task_queue if t.get("model_type") == "CNN")
            lstm_rnn_count = sum(1 for t in gcs_state.task_queue if t.get("model_type") in ["LSTM", "RNN"])
        if q_len_real == 0:
            w_mix = 0
        elif cnn_count > 0 and lstm_rnn_count == 0:
            w_mix = 1
        elif lstm_rnn_count > 0 and cnn_count == 0:
            w_mix = 2
        else:
            w_mix = 3

        with gcs_state.registry_lock:
            w1_idle = 1 if any(info["node_type"] == "on_demand" and info["status"] == "IDLE" for info in gcs_state.worker_registry.values()) else 0
            w2_idle = 1 if any(info["node_type"] == "spot_a" and info["status"] == "IDLE" for info in gcs_state.worker_registry.values()) else 0
            w3_idle = 1 if any(info["node_type"] == "spot_b" and info["status"] == "IDLE" for info in gcs_state.worker_registry.values()) else 0
        a_mix = (w1_idle * 1) + (w2_idle * 2) + (w3_idle * 4)
            
        u_sla = 0
        peek_task = None
        with gcs_state.queue_lock:
            for task in gcs_state.task_queue:
                deps_met = True
                for dep in task.get("dependencies", []):
                    if not gcs_state.completed_tasks_cache.get(dep, False):
                        deps_met = False
                        break
                if deps_met:
                    peek_task = task
                    break
        
        if peek_task:
            time_left = peek_task["deadline"] - time.time()
            if time_left <= 30.0:
                u_sla = 1

        b_avail = 0 if gcs_state.virtual_budget < 0.7 else 1
        state = (w_mix, a_mix, u_sla, b_avail)

        # 6대 가용 행동 매핑
        available_actions = [3]  # HOLD (3)
        
        if peek_task:
            with gcs_state.registry_lock:
                if any(info["node_type"] == "on_demand" and info["status"] == "IDLE" for info in gcs_state.worker_registry.values()):
                    available_actions.append(0)
                if any(info["node_type"] == "spot_a" and info["status"] == "IDLE" for info in gcs_state.worker_registry.values()):
                    available_actions.append(1)
                if any(info["node_type"] == "spot_b" and info["status"] == "IDLE" for info in gcs_state.worker_registry.values()):
                    available_actions.append(2)
                    
        if run_scale_decisions and spot_scale < MAX_SPOT_SCALE:
            available_actions.append(4)  # SCALE_OUT_SPOT_A
            available_actions.append(5)  # SCALE_OUT_SPOT_B

        # Action Masking (가상 예산 부족 시 고비용 액션 필터링)
        if gcs_state.virtual_budget <= 0.0:
            if 0 in available_actions:
                available_actions.remove(0)
            if 4 in available_actions:
                available_actions.remove(4)

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
                threading.Thread(
                    target=run_task_on_worker,
                    args=(worker_id, worker_info, target_task, state, action),
                    daemon=True
                ).start()
            else:
                # 백필링 적용: 현재 선택된 액션 타입의 노드가 가용하지 않으므로 보류하고 탐크 탐색 계속 진행
                deferred_tasks.append(target_task)
            
        elif action == 3:
            dashboard.log_event(f"[Q-Learning Action] HOLD 상태 선택 (대기열 크기: {q_len_real})")
            if gcs_state.Q_LEARNING_TRAINING_MODE:
                reward = -15.0 if u_sla == 1 else 1.0
                agent.update_q_value(state, action, reward, state)
                agent.save_q_table()
                from head.scheduler.task_executor import log_online_training
                log_online_training(state, action, reward, state, agent.epsilon)
                dashboard.log_event(f"[Q-Learning Update] State={state} | Action={action} (HOLD) | Reward={reward:.4f} | Epsilon={agent.epsilon:.4f}")
            break
            
        elif action == 4:
            dashboard.log_event(f"[Q-Learning Action] SCALE_OUT_SPOT_A 트리거 -> Spot-A 노드 추가 증설")
            if gcs_state.Q_LEARNING_TRAINING_MODE:
                reward = 4.0 if u_sla == 1 else -1.5
                if b_avail == 0:
                    reward -= 3.0
                else:
                    reward += 3.0
                agent.update_q_value(state, action, reward, state)
                agent.save_q_table()
                from head.scheduler.task_executor import log_online_training
                log_online_training(state, action, reward, state, agent.epsilon)
                dashboard.log_event(f"[Q-Learning Update] State={state} | Action={action} (SCALE_SPOT_A) | Reward={reward:.4f} | Epsilon={agent.epsilon:.4f}")
            
            if q_len_real >= 6 and spot_scale < MAX_SPOT_SCALE - 1:
                dashboard.log_event(f"[Q-Learning Scale-Out] 대기 큐 심각 적체({q_len_real}개) -> Spot-A 2대 동시 증설")
                if cluster_manager.scale_out_worker("spot_a"):
                    spot_scale += 1
                if cluster_manager.scale_out_worker("spot_a"):
                    spot_scale += 1
            else:
                if cluster_manager.scale_out_worker("spot_a"):
                    spot_scale += 1
            break
            
        elif action == 5:
            dashboard.log_event(f"[Q-Learning Action] SCALE_OUT_SPOT_B 트리거 -> Spot-B 노드 추가 증설")
            if gcs_state.Q_LEARNING_TRAINING_MODE:
                reward = 1.0 if u_sla == 1 else 0.0
                if b_avail == 0:
                    reward += 2.0
                agent.update_q_value(state, action, reward, state)
                agent.save_q_table()
                from head.scheduler.task_executor import log_online_training
                log_online_training(state, action, reward, state, agent.epsilon)
                dashboard.log_event(f"[Q-Learning Update] State={state} | Action={action} (SCALE_SPOT_B) | Reward={reward:.4f} | Epsilon={agent.epsilon:.4f}")

            if q_len_real >= 6 and spot_scale < MAX_SPOT_SCALE - 1:
                dashboard.log_event(f"[Q-Learning Scale-Out] 대기 큐 심각 적체({q_len_real}개) -> Spot-B 2대 동시 증설")
                if cluster_manager.scale_out_worker("spot_b"):
                    spot_scale += 1
                if cluster_manager.scale_out_worker("spot_b"):
                    spot_scale += 1
            else:
                if cluster_manager.scale_out_worker("spot_b"):
                    spot_scale += 1
            break
            
    # 보류된 태스크들의 순서를 유지하여 대기열로 환원 복원
    if deferred_tasks:
        with gcs_state.queue_lock:
            for task in reversed(deferred_tasks):
                gcs_state.task_queue.insert(0, task)
                
    return empty_queue_duration

