"""WE-MEET: 동적 부하 인지형 스케줄링 모듈 (head/scheduler/dynamic.py)

[철학] 인간이 짤 수 있는 최고 수준의 Task↔Node 매칭 하드코딩 — '안전 우선(Safety-First)'.

[노드 성격 (cost_model.yaml 신물리 기준)]
  - on_demand : 최고속(gpu 1.0) · 회수 0% · 고비용        → 안전한 고속 워크호스
  - spot_a    : 빠름(gpu 0.6)   · 회수 0.50(고위험) · 중비용 → 이제 '회수 룰렛' 노드(최후수단)
  - spot_b    : 저속(gpu 0.5)   · 회수 0.10(안전) · 저비용  · 단, LSTM에 OOM 0.20(금지)

[매칭 원칙] Spot-A의 회수가 0.30→0.50으로 올라 '빠르지만 위험'해졌으므로, 회수 낭비를 피하기 위해
  안전 노드(OD·Spot-B)를 우선하고 Spot-A는 최후수단으로 미룬다.
  - LSTM(메모리 폭식) -> On-Demand 우선, Spot-A 폴백. Spot-B는 OOM(0.20)로 '하드 금지'.
  - CNN(무거운 연산)  -> On-Demand 우선(빠르고 안전), Spot-B(안전·저가) 차선, Spot-A 최후.
  - RNN(초경량)       -> Spot-B 우선(저가·안전, 경량이라 저속 감내), OD 차선, Spot-A 최후.
  증설 타입도 안전 우선: 기본 Spot-B(안전·저가), 단 LSTM이 큐에 있으면 Spot-A(LSTM은 Spot-B 금지라).

[보류 완화] 선호 노드가 다 바빠도 '하드 금지가 아닌 유휴 노드'가 있으면 즉시 배정한다(선호 순서는 유지).
  선호 노드가 빌 때까지 대기하다 큐가 적체되어 마감을 놓치던 문제를 제거한다.
"""

import time
import threading
import wemeet.cluster.gcs_state as gcs_state
import wemeet.cluster.manager as cluster_manager
from wemeet.observability import logging as _obslog
from wemeet.config import env_config as _ec

# 스케줄러 정책 값의 유일 진실: sim_env.yaml scheduler_policy (env_config).
_SP = _ec.scheduler_policy()

# 모델별 선호 노드 타입 순서 (앞쪽일수록 우선). 안전 우선: Spot-A(회수 0.50)는 어느 모델에서도 최후순위.
MODEL_NODE_PREFERENCE = _SP["model_node_preference"]

# 모델별 '하드 금지' 노드 — 보류 완화(폴백)로도 절대 배정하지 않는다(LSTM→Spot-B OOM).
# yaml 은 리스트로 보관하므로 `in` 판정 의미를 유지하기 위해 set 으로 변환한다.
MODEL_FORBIDDEN = {m: set(v) for m, v in _SP["model_forbidden"].items()}

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
            num_lstm = sum(1 for t in gcs_state.task_queue if t.get("model_type") == "LSTM")

        if active_workers:
            avg_cpu = sum(info.get("cpu", 0.0) for info in active_workers) / len(active_workers)
            avg_mem = sum(info.get("mem", 0.0) for info in active_workers) / len(active_workers)
            active_spot_a = sum(1 for info in active_workers if info.get("node_type") == "spot_a")
        else:
            avg_cpu, avg_mem = 0.0, 0.0
            active_spot_a = 0

        # 스마트 스케일아웃 결정: 큐 내의 LSTM 요구 개수가 현재 가동 중인 spot_a 대수보다 많을 때만 Spot-A 증설
        # 그 외의 일반 적체 상황에서는 요금이 저렴하고 안전한 Spot-B를 집중 기동하여 자원 효율을 극대화
        target_type = "spot_a" if (num_lstm > active_spot_a) else "spot_b"

        if q_len_real >= _SP["scale_out_burst_qlen"] and spot_scale < MAX_SPOT_SCALE - 1:
            _obslog.log_event(f"[Dynamic Scale-Out] 대기 큐 심각 적체({q_len_real}개) -> Spot-{target_type[-1].upper()} 노드 2대 동시 증설")
            if cluster_manager.scale_out_worker(target_type):
                spot_scale += 1
            if cluster_manager.scale_out_worker(target_type):
                spot_scale += 1
        elif ((avg_cpu > 70.0 or avg_mem > 70.0) or q_len_real >= _SP["scale_out_normal_qlen"]) and spot_scale < MAX_SPOT_SCALE:  # SLA 상향을 위해 적체 기준 완화
            _obslog.log_event(f"[Dynamic Scale-Out] 대기 큐 적체({q_len_real}개) 또는 고부하 감지 -> Spot-{target_type[-1].upper()} 노드 1대 증설")
            if cluster_manager.scale_out_worker(target_type):
                spot_scale += 1

        if q_len_real == 0 and avg_cpu < 20.0 and avg_mem < 20.0:
            scale_in_timer += 1.0
            if scale_in_timer >= _SP["scale_in_sec_dynamic"] and spot_scale > 0:  # 플래핑으로 인한 cold start 방지를 위해 축소 유예(기본 15s)
                # 요금이 더 비싼 spot_a를 우선 회수하여 예산 효율을 최적화
                with gcs_state.registry_lock:
                    has_spot_a = any(info.get("node_type") == "spot_a" for info in gcs_state.worker_registry.values())

                reclaim_type = "spot_a" if has_spot_a else "spot_b"
                _obslog.log_event(f"[Dynamic Scale-In] 저부하 유휴 상태 3초 유지 -> Spot-{reclaim_type[-1].upper()} 워커 회수")
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
        pref = MODEL_NODE_PREFERENCE.get(model, ["on_demand", "spot_b", "spot_a"])
        forbidden = MODEL_FORBIDDEN.get(model, set())

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
                # 하드 금지 노드는 폴백으로도 절대 배정하지 않는다 (예: LSTM->Spot-B OOM).
                if ntype in forbidden:
                    continue
                # 정렬 1순위 = 선호 순위(선호 목록에 없으면 맨 뒤로 밀어 '폴백'으로만 쓰임),
                #        2순위 = least-loaded. → 선호 노드가 유휴면 그걸, 아니면 금지 아닌 유휴 노드에 즉시 배정(보류 완화).
                rank = pref.index(ntype) if ntype in pref else len(pref)
                candidates.append((rank, cpu_val * 0.5 + mem_val * 0.5, wid, info))

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
            # 보류 완화 적용 후에도 배정 못 함 = 배정 가능한 유휴 노드가 전무(모두 BUSY/과부하이거나
            # 남은 유휴가 하드 금지 노드뿐). 이 경우에만 백필링(보류). 스팸 방지 로그.
            cur_time = time.time()
            last_log = getattr(run_dynamic_scheduler_step, "_last_log_time", 0.0)
            if cur_time - last_log >= 5.0:
                _obslog.log_event(f"[Dynamic Staggered] 배정 보류: {target_task['task_id']}({model}) 가용 유휴 노드 없음 -> 증설/완료 대기 (대기 중)")
                run_dynamic_scheduler_step._last_log_time = cur_time
            deferred_tasks.append(target_task)

    # 보류된 태스크들의 순서를 유지하여 대기열 선두로 복원
    if deferred_tasks:
        with gcs_state.queue_lock:
            for task in reversed(deferred_tasks):
                gcs_state.task_queue.insert(0, task)

    return scale_in_timer
