import os
import sys
import time
import grpc
import threading
import random

# 표준 출력 버퍼 비우기 (Flush) 설정
import builtins
_original_print = builtins.print
def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    _original_print(*args, **kwargs)
builtins.print = print

# GCS 전역 인메모리 스토어 상태 임포트
import head.state as gcs_state
import head.dashboard.server as dashboard
from proto import babyray_pb2
from proto import babyray_pb2_grpc
from head.q_learning.agent import QLearningAgent

# [Q-Learning Agent 싱글톤 인스턴스 모듈화]
# - scheduler.py와 scheduler/core.py가 각각 생성하던 에이전트를 공통 유틸로 통합하여 
#   비용 모델과 학습 Q-Table 인스턴스의 메모리 정합성 및 일관성을 확보합니다.
COST_MODEL_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../common/cost_model.yaml'))
agent = QLearningAgent(cost_model_path=COST_MODEL_PATH)

def log_benchmark_metric(scheduler_mode, task_id, model_type, status, execution_time, cost, delay, virtual_budget):
    """
    data/benchmark_results.csv 파일에 3대 스케줄러의 성능 비교 데이터를 누적 기록합니다.
    """
    csv_file = "data/benchmark_results.csv"
    try:
        os.makedirs(os.path.dirname(csv_file), exist_ok=True)
        file_exists = os.path.exists(csv_file)
        
        # 기동 중인 워커 수 계측
        active_od = 0
        active_spot_a = 0
        active_spot_b = 0
        with gcs_state.registry_lock:
            for info in gcs_state.worker_registry.values():
                ntype = info.get("node_type", "on_demand").lower()
                if ntype == "on_demand":
                    active_od += 1
                elif ntype == "spot_a":
                    active_spot_a += 1
                elif ntype == "spot_b":
                    active_spot_b += 1
                    
        import csv
        with open(csv_file, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                # 헤더 작성
                writer.writerow([
                    "timestamp", "scheduler_mode", "task_id", "model_type", "status",
                    "execution_time", "cost", "delay", "active_od", "active_spot_a",
                    "active_spot_b", "virtual_budget"
                ])
            # 데이터 로깅
            writer.writerow([
                time.strftime("%Y-%m-%d %H:%M:%S"),
                scheduler_mode,
                task_id,
                model_type,
                status,
                round(execution_time, 4),
                round(cost, 6),
                round(delay, 4),
                active_od,
                active_spot_a,
                active_spot_b,
                round(virtual_budget, 6)
            ])
    except Exception as e:
        print(f"[Benchmark Logger 경고] 결과 CSV 파일 저장 실패: {e}")

def adjust_worker_resources(wid, wtype, model_type, is_restore=False, channel=None):
    """
    태스크 모델 유형에 따라 워커 노드의 cGroup 리소스 한도를 동적으로 조정합니다.
    - LSTM(메모리 집약형): 메모리 한도를 1.5배 임시 확장
    - CNN/RNN/MERGE: 기본 규격 크기로 유지/복원
    """
    # 온디맨드 노드(worker-1)는 동적 리사이징 대상에서 제외하여 정적 2GB 상태를 안정적으로 유지합니다.
    if wid == "worker-1":
        return
        
    cost_profile = agent.nodes_config.get(wtype, {})
    cpu_limit = cost_profile.get("cpu_limit", 1.0)
    mem_limit_mb = cost_profile.get("memory_limit_mb", 1024)
    
    if not is_restore and model_type.upper() == "LSTM":
        target_mem = int(mem_limit_mb * 1.5)
    else:
        target_mem = mem_limit_mb
    target_cpu = cpu_limit
    
    # 1. 호스트 Docker cGroup 리사이징 수행
    import head.cluster_manager as cluster_manager
    cluster_manager.resize_worker_resources(wid, target_cpu, target_mem)
    
    # 2. 워커 원격 노티 gRPC 통신 전송
    if channel:
        try:
            stub = babyray_pb2_grpc.BabyRayServiceStub(channel)
            stub.ResizeResources(babyray_pb2.ResizeRequest(
                cpu_cores=target_cpu,
                memory_bytes=target_mem * 1024 * 1024
            ), timeout=1.0)
        except Exception as e:
            print(f"[gRPC Resize 경고] {wid} 자원 조정 노티 실패: {e}")

def run_task_on_worker(worker_id, worker_info, task, state, action):
    """
    [Task 실행 및 강화학습 피드백 스레드 (공통 유틸리티)]
    특정 워커에 작업을 할당하여 gRPC로 실행 지시를 내리고 완료 모니터링 후 보상(Reward)을 계산하여 Q-Table을 갱신합니다.
    epochs 가 8 이상이고 가용 IDLE 노드가 2대 이상일 경우 병렬 Map-Merge 연산으로 확장 분할 구동합니다.
    """
    ip = worker_info['ip']
    worker_address = f"[{ip}]:{worker_info['port']}" if ":" in ip else f"{ip}:{worker_info['port']}"
    task_id = task["task_id"]
    success = False
    execution_time = 0.0
    start_time = time.time()
    
    try:
        model_type = task["model_type"]
        epochs = task["epochs"]
        
        # 현재 GCS에 등록된 가용 IDLE 워커 리스트 스캔
        with gcs_state.registry_lock:
            available_idle_workers = [
                (wid, info) for wid, info in gcs_state.worker_registry.items()
                if info["status"] == "IDLE" and wid != worker_id
            ]
            if worker_id in gcs_state.worker_registry:
                available_idle_workers.append((worker_id, gcs_state.worker_registry[worker_id]))
            # worker-1 (온디맨드 노드)가 리스트에 있다면 최우선(index 0)으로 오도록 정렬 (병렬 맵 실패 최소화)
            available_idle_workers.sort(key=lambda x: 0 if x[0] == "worker-1" else 1)
                
        is_map_reduce = (
            model_type.upper() in ["CNN", "RNN", "LSTM"]
            and epochs >= 8
            and len(available_idle_workers) >= 2
            and not task.get("is_recovered_subtask", False)
        )
        
        if is_map_reduce:
            # 원래 이 스레드의 주체 워커(worker_id)는 전체 Map-Reduce 동안 BUSY 상태를 유지해야
            # 백그라운드 연산 도중 스케줄러가 다른 일반 작업을 새롭게 할당하는 것을 원천 차단합니다.
            pass

            dashboard.log_event(f"[Map-Merge] {task_id} 병렬 학습 분할 개시. 가용 IDLE 워커 수: {len(available_idle_workers)}")
            
            # 1. 쪼갤 대수 결정 (최대 3분할)
            num_splits = min(len(available_idle_workers), 3)
            sub_epochs = epochs // num_splits
            remainder = epochs % num_splits
            
            map_results = {}
            
            def execute_map_subtask(sub_idx, w_id, w_info):
                sub_task_id = f"{task_id}-map-{sub_idx}"
                sub_ep = sub_epochs + (remainder if sub_idx == 0 else 0)
                
                # 1. Skip-Execution 검사: 이미 최종 가중치 파일이 성공적으로 존재하면 즉시 스킵
                final_pt = f"data/final_{sub_task_id}.pt"
                if os.path.exists(final_pt) and os.path.getsize(final_pt) > 0:
                    dashboard.log_event(f"[Map Skip] 서브맵 {sub_task_id} 이미 완료됨 -> 학습 Skip-Execution 처리.")
                    inf_log = f"[{model_type} Inference/Forecast/Generation Done] (Skip-Execution 복구본)"
                    map_results[sub_idx] = {
                        "success": True,
                        "worker_id": w_id,
                        "worker_type": w_info["node_type"],
                        "execution_time": 0.0,
                        "output_file": final_pt,
                        "inference_log": inf_log
                    }
                    with gcs_state.registry_lock:
                        if sub_task_id in gcs_state.task_lineage:
                            gcs_state.task_lineage[sub_task_id]["status"] = "SUCCESS"
                        if w_id in gcs_state.worker_registry:
                            if w_id != worker_id:
                                gcs_state.worker_registry[w_id]["status"] = "IDLE"
                    gcs_state.save_gcs_state()
                    return

                # 2. Re-execution 검사: 중간 체크포인트가 존재하면 로드 및 남은 에포크만 이어서 실행
                last_epoch = 0
                checkpoint_file = None
                for ep in range(sub_ep, 0, -1):
                    chk_path = f"data/checkpoint_{sub_task_id}_epoch_{ep}.pt"
                    if os.path.exists(chk_path):
                        last_epoch = ep
                        checkpoint_file = chk_path
                        break

                actual_ep = sub_ep
                sub_dataset_path = task.get("dataset_path", f"data/{model_type.lower()}_dataset.pt")
                if task.get("dataset_path", "").endswith(".pt"):
                    sub_dataset_path = task["dataset_path"]

                if last_epoch > 0 and last_epoch < sub_ep:
                    actual_ep = sub_ep - last_epoch
                    sub_dataset_path = checkpoint_file
                    dashboard.log_event(f"[장애 복구] 서브맵 {sub_task_id} 중단 감지 -> {last_epoch} Epoch 가중치를 기반으로 이어서 학습 복구(남은 {actual_ep} Epochs) 시작.")

                sub_ip = w_info['ip']
                sub_address = f"[{sub_ip}]:{w_info['port']}" if ":" in sub_ip else f"{sub_ip}:{w_info['port']}"
                
                dashboard.log_event(f"[Map Task] 서브맵 할당: {sub_task_id} ({model_type}, {actual_ep} Ep) -> 워커 {w_id}")
                
                sub_success = False
                sub_start = time.time()
                inference_log = None
                try:
                    sub_channel = grpc.insecure_channel(sub_address)
                    sub_stub = babyray_pb2_grpc.BabyRayServiceStub(sub_channel)
                    
                    # 동적 cGroup 리소스 리사이징 적용
                    adjust_worker_resources(w_id, w_info["node_type"], model_type, is_restore=False, channel=sub_channel)
                    
                    res = sub_stub.AssignTask(babyray_pb2.TaskAssignment(
                        task_id=sub_task_id,
                        model_type=model_type,
                        dataset_path=sub_dataset_path,
                        epochs=actual_ep
                    ))
                    
                    if res.status == "RUNNING":
                        while True:
                            time.sleep(1.0)
                            with gcs_state.registry_lock:
                                if w_id not in gcs_state.worker_registry:
                                    raise grpc.RpcError("Worker went offline during subtask")
                                    
                            stat = sub_stub.GetTaskStatus(babyray_pb2.TaskStatusRequest(task_id=sub_task_id))
                            if stat.status in ["SUCCESS", "COMPLETED"]:
                                sub_success = True
                                if stat.logs:
                                    for line in stat.logs.splitlines():
                                        if line.strip():
                                            dashboard.log_event(f"[{w_id}] {line.strip()}")
                                            if any(tag in line for tag in ["[CNN Inference Done]", "[RNN Forecast Done]", "[LSTM Generation Done]"]):
                                                inference_log = line.strip()
                                math_concl = ""
                                with gcs_state.registry_lock:
                                    if sub_task_id in gcs_state.task_lineage:
                                        gcs_state.task_lineage[sub_task_id]["status"] = "SUCCESS"
                                gcs_state.save_gcs_state()
                                break
                            elif stat.status == "FAILED":
                                sub_success = False
                                with gcs_state.registry_lock:
                                    if sub_task_id in gcs_state.task_lineage:
                                        gcs_state.task_lineage[sub_task_id]["status"] = "FAILED"
                                gcs_state.save_gcs_state()
                                break
                except Exception as ex:
                    dashboard.log_event(f"[Map Task 에러] 서브맵 {sub_task_id} (워커 {w_id}) 실패: {ex}")
                    sub_success = False
                    with gcs_state.registry_lock:
                        if sub_task_id in gcs_state.task_lineage:
                            gcs_state.task_lineage[sub_task_id]["status"] = "FAILED"
                    gcs_state.save_gcs_state()
                finally:
                    # 동적 cGroup 리소스 원복 복원
                    try:
                        adjust_worker_resources(w_id, w_info["node_type"], model_type, is_restore=True, channel=sub_channel)
                    except Exception:
                        pass
                        
                    with gcs_state.registry_lock:
                        if w_id in gcs_state.worker_registry:
                            # 주체 워커(worker_id)는 Map-Reduce가 완전히 끝날 때까지 IDLE로 복구하지 않고 점유 상태(BUSY)를 유지합니다.
                            if w_id != worker_id:
                                gcs_state.worker_registry[w_id]["status"] = "IDLE"
                    
                    map_results[sub_idx] = {
                        "success": sub_success,
                        "worker_id": w_id,
                        "worker_type": w_info["node_type"],
                        "execution_time": time.time() - sub_start,
                        "output_file": f"data/final_{sub_task_id}.pt",
                        "inference_log": inference_log
                    }

            # 아키텍처 개선: 스팟 회수(Eviction)에 따른 전체 무한 롤백을 피하기 위한 선택적 부분 재시도(Selective Retry) 루프
            max_retries = 5
            all_maps_success = False
            
            for attempt in range(max_retries):
                pending_indices = [i for i in range(num_splits) if i not in map_results or not map_results[i]["success"]]
                if not pending_indices:
                    all_maps_success = True
                    break
                
                dashboard.log_event(f"[Map-Merge] {task_id} 시도 {attempt+1}/{max_retries} | 미완료 맵 서브태스크: {pending_indices}")
                
                # 매 시도마다 실시간 가용 IDLE 워커 재수집
                with gcs_state.registry_lock:
                    available_idle_workers = [
                        (wid, info) for wid, info in gcs_state.worker_registry.items()
                        if info["status"] == "IDLE"
                    ]
                
                if not available_idle_workers:
                    time.sleep(2.0)
                    continue
                
                # 안정성 강화를 위해 worker-1(온디맨드) 노드가 가용하다면 최우선으로 매핑 정렬
                available_idle_workers.sort(key=lambda x: 0 if x[0] == "worker-1" else 1)
                
                map_threads = []
                
                for idx_in_pending, sub_idx in enumerate(pending_indices):
                    if idx_in_pending >= len(available_idle_workers):
                        break # 가용 워커가 소진되면 다음 시도로 이월
                        
                    wid, winfo = available_idle_workers[idx_in_pending]
                    sub_task_id = f"{task_id}-map-{sub_idx}"
                    
                    with gcs_state.registry_lock:
                        if wid in gcs_state.worker_registry:
                            gcs_state.worker_registry[wid]["status"] = f"MAP ({sub_task_id})"
                            
                    sub_ep = sub_epochs + (remainder if sub_idx == 0 else 0)
                    
                    with gcs_state.registry_lock:
                        gcs_state.task_lineage[sub_task_id] = {
                            "parent": task_id,
                            "model_type": model_type,
                            "epochs": sub_ep,
                            "worker_id": wid,
                            "status": "RUNNING",
                            "dataset_path": task.get("dataset_path", "")
                        }
                    gcs_state.save_gcs_state()
                    
                    t = threading.Thread(target=execute_map_subtask, args=(sub_idx, wid, winfo))
                    t.start()
                    map_threads.append(t)
                    
                for t in map_threads:
                    t.join()
                    
            if not all_maps_success:
                all_maps_success = len(map_results) == num_splits and all(r["success"] for r in map_results.values())
            
            if not all_maps_success:
                dashboard.log_event(f"[Map-Merge] 경고: 최대 재시도 한도 초과로 일부 맵 태스크가 최종 실패했습니다. 복구 복구 루프 재진입.")
                success = False
                execution_time = time.time() - start_time
            else:
                merge_files = [r["output_file"] for r in map_results.values()]
                merge_dataset_path = f"merge:" + ",".join(merge_files)
                reduce_task_id = f"{task_id}-merge"
                
                with gcs_state.registry_lock:
                    reduce_candidates = [
                        (wid, info) for wid, info in gcs_state.worker_registry.items()
                        if info["status"] == "IDLE"
                    ]
                
                # Reduce는 회수(Eviction) 위험이 없는 안정적인 on_demand 노드로 전용 고정(Pinning)합니다.
                reduce_worker_id = None
                reduce_worker_info = None
                
                # 만약 원래 주체 워커가 온디맨드 노드라면 바로 지정하여 상태 경쟁을 방지합니다.
                with gcs_state.registry_lock:
                    if worker_id in gcs_state.worker_registry and gcs_state.worker_registry[worker_id]["node_type"] == "on_demand":
                        reduce_worker_id = worker_id
                        reduce_worker_info = gcs_state.worker_registry[worker_id].copy()
                        gcs_state.worker_registry[reduce_worker_id]["status"] = f"MERGE ({reduce_task_id})"
                
                if not reduce_worker_id:
                    while True:
                        with gcs_state.registry_lock:
                            on_demand_candidates = [
                                (wid, info) for wid, info in gcs_state.worker_registry.items()
                                if info["node_type"] == "on_demand" and info["status"] == "IDLE"
                            ]
                            if on_demand_candidates:
                                reduce_worker_id, reduce_worker_info = on_demand_candidates[0]
                                gcs_state.worker_registry[reduce_worker_id]["status"] = f"MERGE ({reduce_task_id})"
                                break
                        dashboard.log_event(f"[Merge Task] 온디맨드 가용 IDLE 워커(worker-1) 대기 중...")
                        time.sleep(1.0)
                
                if reduce_worker_id:
                    r_stat = None
                    reduce_success = False
                    reduce_ip = reduce_worker_info['ip']
                    reduce_address = f"[{reduce_ip}]:{reduce_worker_info['port']}" if ":" in reduce_ip else f"{reduce_ip}:{reduce_worker_info['port']}"
                    
                    dashboard.log_event(f"[Merge Task] 병합 가중치 생성 트리거: {reduce_task_id} -> 워커 {reduce_worker_id}")
                    try:
                        r_channel = grpc.insecure_channel(reduce_address)
                        r_stub = babyray_pb2_grpc.BabyRayServiceStub(r_channel)
                        
                        r_res = r_stub.AssignTask(babyray_pb2.TaskAssignment(
                            task_id=task_id,
                            model_type="MERGE",
                            dataset_path=merge_dataset_path,
                            epochs=1
                        ))
                        
                        if r_res.status == "RUNNING":
                            while True:
                                time.sleep(1.0)
                                with gcs_state.registry_lock:
                                    if reduce_worker_id not in gcs_state.worker_registry:
                                        raise grpc.RpcError("Reduce worker went offline")
                                        
                                r_stat = r_stub.GetTaskStatus(babyray_pb2.TaskStatusRequest(task_id=task_id))
                                if r_stat.status in ["SUCCESS", "COMPLETED"]:
                                    reduce_success = True
                                    if r_stat.logs:
                                        for line in r_stat.logs.splitlines():
                                            if line.strip():
                                                dashboard.log_event(f"[{reduce_worker_id}] {line.strip()}")
                                    break
                                elif r_stat.status == "FAILED":
                                    reduce_success = False
                                    break
                    except Exception as rex:
                        dashboard.log_event(f"[Merge Task 에러] FedAvg 병합 실패: {rex}")
                        reduce_success = False
                    finally:
                        with gcs_state.registry_lock:
                            if reduce_worker_id in gcs_state.worker_registry:
                                gcs_state.worker_registry[reduce_worker_id]["status"] = "IDLE"
                                
                    success = reduce_success
                    execution_time = time.time() - start_time
                    if success:
                        dashboard.log_event(f"[Map-Merge] {task_id} 최종 Map-Merge FedAvg 병합 성공! (총 시간: {execution_time:.2f}초)")
                        
                        # --- 최종 결론 도출 및 결합 로직 ---
                        try:
                            import re
                            merged_conclusion = ""
                            
                            # REDUCE/MERGE 워커 로그에서 [FedAvg Verification] 라인을 직접 추출하여 최종 결론으로 사용
                            if r_stat and r_stat.logs:
                                for line in r_stat.logs.splitlines():
                                    if "[FedAvg Verification]" in line:
                                        verification_content = line.split("[FedAvg Verification]")[-1].strip()
                                        merged_conclusion = f"[FedAvg 분산 병합 결론] 수학적 가중치 결합 모델 추론 결과 -> {verification_content}"
                                        break
                                        
                            # 만약 로그에서 추출하지 못했다면 Fallback으로 기존의 개별 맵 결과 병합 로직 사용
                            if not merged_conclusion:
                                logs_to_merge = [r.get("inference_log") for r in map_results.values() if r and r.get("inference_log")]
                                
                                if model_type.upper() == "CNN":
                                    classes = []
                                    confs = []
                                    for log in logs_to_merge:
                                        m = re.search(r"예측 클래스:\s*(\d+)\s*\(신뢰도:\s*([\d.]+)%\)", log)
                                        if m:
                                            classes.append(int(m.group(1)))
                                            confs.append(float(m.group(2)))
                                    if classes:
                                        from collections import Counter
                                        majority_class = Counter(classes).most_common(1)[0][0]
                                        avg_conf = sum(confs) / len(confs)
                                        merged_conclusion = f"[CNN 분산 병합 결론] 다수결 이미지 분석 결과 -> 최종 예측 클래스: {majority_class} (평균 신뢰도: {avg_conf:.2f}%)"
                                        
                                elif model_type.upper() == "RNN":
                                    all_forecasts = []
                                    for log in logs_to_merge:
                                        m = re.search(r"예측값\s*->\s*\[(.*?)\]", log)
                                        if m:
                                            vals = [float(v.strip()) for v in m.group(1).split(",")]
                                            all_forecasts.append(vals)
                                    if all_forecasts:
                                        steps = len(all_forecasts[0])
                                        avg_forecasts = []
                                        for step in range(steps):
                                            step_vals = [f[step] for f in all_forecasts if len(f) > step]
                                            avg_forecasts.append(sum(step_vals) / len(step_vals))
                                        avg_forecasts_str = ", ".join([f"{v:.3f}" for v in avg_forecasts])
                                        merged_conclusion = f"[RNN 분산 병합 결론] 예측 수치 FedAvg 평균값 -> [{avg_forecasts_str}]"
                                        
                                elif model_type.upper() == "LSTM":
                                    text_fragments = []
                                    for log in logs_to_merge:
                                        m = re.search(r"텍스트 생성 결과\s*->\s*\"(.*?)\"", log)
                                        if m:
                                            text_fragments.append(m.group(1))
                                    if text_fragments:
                                        joined_text = " | ".join(text_fragments)
                                        merged_conclusion = f"[LSTM 분산 병합 결론] 이종 분할 텍스트 병합 -> \"{joined_text}\""
                                    
                            if merged_conclusion:
                                dashboard.log_event(f"[Conclusion Engine] {merged_conclusion}")
                                gcs_state.latest_conclusions.append({
                                    "task_id": task_id,
                                    "model_type": model_type,
                                    "timestamp": time.time(),
                                    "conclusion": merged_conclusion
                                })
                        except Exception as e_conclusion:
                            dashboard.log_event(f"[Conclusion Engine 경고] 결론 도출 및 병합 중 오류: {e_conclusion}")

                        # 1. task_lineage 딕셔너리에서 완료된 맵 족보 정리 (메모리 릭 방지)
                        with gcs_state.registry_lock:
                            for sub_id in list(gcs_state.task_lineage.keys()):
                                lineage_info = gcs_state.task_lineage.get(sub_id)
                                if lineage_info and lineage_info.get("parent") == task_id:
                                    if sub_id in gcs_state.task_lineage:
                                        del gcs_state.task_lineage[sub_id]
                        gcs_state.save_gcs_state()
                                    
                        # 2. 중간 맵 가중치 파일 (.pt) 즉각 소거 (WSL2/도커 공간 누수 방지)
                        for f_path in merge_files:
                            try:
                                if os.path.exists(f_path):
                                    os.remove(f_path)
                                    dashboard.log_event(f"[Merge Cleanup] 임시 맵 가중치 파일 소거 완료: {f_path}")
                            except Exception as cleanup_err:
                                dashboard.log_event(f"[Merge Cleanup 경고] 파일 {f_path} 소거 중 오류: {cleanup_err}")
                                
                        # 2-1. 중간 맵 체크포인트 가중치 파일 (.pt) 즉각 소거
                        import glob
                        map_checkpoints = glob.glob(f"data/checkpoint_{task_id}-map-*_epoch_*.pt")
                        for cp_path in map_checkpoints:
                            try:
                                if os.path.exists(cp_path):
                                    os.remove(cp_path)
                                    dashboard.log_event(f"[Merge Cleanup] 임시 맵 체크포인트 파일 소거 완료: {cp_path}")
                            except Exception as cleanup_err:
                                dashboard.log_event(f"[Merge Cleanup 경고] 파일 {cp_path} 소거 중 오류: {cleanup_err}")
                                
                        # 3. 최종 병합된 가중치 파일 (.pt) 즉각 소거 (WSL2/도커 공간 누수 방지)
                        final_merged_path = f"data/final_{task_id}.pt"
                        try:
                            if os.path.exists(final_merged_path):
                                os.remove(final_merged_path)
                                dashboard.log_event(f"[Merge Cleanup] 최종 병합 가중치 파일 소거 완료: {final_merged_path}")
                        except Exception as cleanup_err:
                            dashboard.log_event(f"[Merge Cleanup 경고] 최종 병합 파일 {final_merged_path} 소거 중 오류: {cleanup_err}")
                    else:
                        dashboard.log_event(f"[Map-Merge] {task_id} Merge 병합 단계 실패.")
                else:
                    dashboard.log_event(f"[Merge Task 에러] 병합을 맡길 가용 워커가 존재하지 않습니다. 실패 처리.")
                    success = False
                    execution_time = time.time() - start_time
        
            # [일반 단일 워커 할당 분기]
            with gcs_state.registry_lock:
                if worker_id in gcs_state.worker_registry:
                    gcs_state.worker_registry[worker_id]["status"] = f"BUSY ({task_id})"
                    
            channel = grpc.insecure_channel(worker_address)
            stub = babyray_pb2_grpc.BabyRayServiceStub(channel)
            
            # [아키텍처 선택: 파일 기반 통신 및 복잡도 절충]
            dataset_path = task.get("dataset_path", f"data/{model_type.lower()}_dataset.pt")
            if model_type.upper() == "MERGE":
                job_id = task.get("job_id", "")
                dataset_path = f"merge:data/final_{job_id}-map-1.pt,data/final_{job_id}-map-2.pt,data/final_{job_id}-map-3.pt"
                
            # 동적 cGroup 리소스 리사이징 적용
            adjust_worker_resources(worker_id, worker_info["node_type"], model_type, is_restore=False, channel=channel)
            
            result = stub.AssignTask(babyray_pb2.TaskAssignment(
                task_id=task_id,
                model_type=model_type,
                dataset_path=dataset_path,
                epochs=epochs
            ))
            
            if result.status == "RUNNING":
                while True:
                    time.sleep(1.5)
                    
                    with gcs_state.registry_lock:
                        if worker_id not in gcs_state.worker_registry:
                            raise grpc.RpcError("Worker node went offline during task execution.")
                    
                    status_res = stub.GetTaskStatus(babyray_pb2.TaskStatusRequest(task_id=task_id))
                    if status_res.status in ["SUCCESS", "COMPLETED"]:
                        success = True
                        execution_time = time.time() - start_time
                        dashboard.log_event(f"[Scheduler Feedback] 작업 {task_id} 완료 성공! (실제 수행 시간: {execution_time:.2f}초)")
                        
                        # 단일 실행 시에도 최종 결론 수집
                        try:
                            if status_res.logs:
                                for line in status_res.logs.splitlines():
                                    if any(tag in line for tag in ["[CNN Inference Done]", "[RNN Forecast Done]", "[LSTM Generation Done]"]):
                                        conclusion_text = f"[{model_type} 단일 실행 결론] {line.strip()}"
                                        dashboard.log_event(f"[Conclusion Engine] {conclusion_text}")
                                        gcs_state.latest_conclusions.append({
                                            "task_id": task_id,
                                            "model_type": model_type,
                                            "timestamp": time.time(),
                                            "conclusion": conclusion_text
                                        })
                        except Exception as e_conclusion:
                            dashboard.log_event(f"[Conclusion Engine 경고] 단일 결론 도출 오류: {e_conclusion}")
                            
                        # 일반 태스크 최종 가중치 파일 소거 (공간 절약)
                        final_task_path = f"data/final_{task_id}.pt"
                        try:
                            if os.path.exists(final_task_path):
                                os.remove(final_task_path)
                                dashboard.log_event(f"[Task Cleanup] 최종 가중치 파일 소거 완료: {final_task_path}")
                        except Exception as cleanup_err:
                            dashboard.log_event(f"[Task Cleanup 경고] 파일 {final_task_path} 소거 중 오류: {cleanup_err}")

                        # 일반 태스크 중간 체크포인트 가중치 파일 소거 (공간 절약)
                        import glob
                        task_checkpoints = glob.glob(f"data/checkpoint_{task_id}_epoch_*.pt")
                        for cp_path in task_checkpoints:
                            try:
                                if os.path.exists(cp_path):
                                    os.remove(cp_path)
                                    dashboard.log_event(f"[Task Cleanup] 체크포인트 파일 소거 완료: {cp_path}")
                            except Exception as cleanup_err:
                                dashboard.log_event(f"[Task Cleanup 경고] 파일 {cp_path} 소거 중 오류: {cleanup_err}")
                            
                        break
                    elif status_res.status == "FAILED":
                        success = False
                        execution_time = time.time() - start_time
                        dashboard.log_event(f"[Scheduler Feedback] 경고: 작업 {task_id} 연산 실패 리포트 수신.")
                        break
            else:
                dashboard.log_event(f"[Scheduler Feedback] 작업 개시 거부당함: {result.message}")
                
    except grpc.RpcError as e:
        dashboard.log_event(f"[Scheduler Feedback 에러] 워커 '{worker_id}' 실행 중 통신 크래시 감지: {e}")
        success = False
        execution_time = time.time() - task["enqueue_time"]
    except Exception as ex:
        import traceback
        tb = traceback.format_exc()
        dashboard.log_event(f"[Scheduler Error] Unexpected error running task {task_id} on {worker_id}: {ex}\n{tb}")
        success = False
        execution_time = time.time() - start_time
    finally:
        # 동적 cGroup 리소스 원복 복원
        try:
            adjust_worker_resources(worker_id, worker_info["node_type"], model_type, is_restore=True, channel=None)
        except Exception:
            pass
            
        # GCS 워커 노드 상태 복구
        with gcs_state.registry_lock:
            if worker_id in gcs_state.worker_registry:
                gcs_state.worker_registry[worker_id]["status"] = "IDLE"
            
            # GCS 전역 작업 상태 정보 업데이트 및 상태 영속 파일로 저장
            if success:
                gcs_state.completed_tasks_cache[task_id] = True
                gcs_state.task_status[task_id] = "SUCCESS"
            else:
                gcs_state.task_status[task_id] = "FAILED"
        gcs_state.save_gcs_state()
                
        # --- Q-Learning 보상 산출 및 Q-Table 업데이트 피드백 단계 ---
        end_time = time.time()
        delay_time = max(0.0, end_time - task["deadline"])
        deadline_exceeded = end_time > task["deadline"]
        
        # 가상 예산 차감 (실시간 초당 비용 모델 도입으로 완료 시 차감은 비활성화합니다)
        worker_type = worker_info["node_type"]
        cost_profile = agent.nodes_config.get(worker_type, {"cost_per_hour": 0.0})
        cost_per_hour = cost_profile.get("cost_per_hour", 0.0)
        task_cost = cost_per_hour * (execution_time / 3600.0)
        # gcs_state.virtual_budget -= task_cost
        
        if gcs_state.SCHEDULER_MODE == "q_learning" and state is not None and action is not None:
            # 동시 기동 중이던 이종 모형 분석 (Co-scheduling 평가용)
            co_scheduled = []
            with gcs_state.registry_lock:
                for sub_id, l_info in gcs_state.task_lineage.items():
                    if l_info.get("worker_id") == worker_id and l_info.get("status") == "RUNNING" and sub_id != task["task_id"]:
                        co_scheduled.append(l_info.get("model_type", ""))
                        
            reward = agent.calculate_reward(
                success=success,
                execution_time=execution_time,
                worker_type=worker_type,
                delay_time=delay_time,
                deadline_exceeded=deadline_exceeded,
                current_model=model_type,
                co_scheduled_models=co_scheduled
            )
            
            # 다음 상태(Next State) 산출 리팩토링
            with gcs_state.queue_lock:
                cnn_count = sum(1 for t in gcs_state.task_queue if t.get("model_type") == "CNN")
                lstm_rnn_count = sum(1 for t in gcs_state.task_queue if t.get("model_type") in ["LSTM", "RNN"])
                q_len_next = len(gcs_state.task_queue)
            if q_len_next == 0:
                w_mix_next = 0
            elif cnn_count > 0 and lstm_rnn_count == 0:
                w_mix_next = 1
            elif lstm_rnn_count > 0 and cnn_count == 0:
                w_mix_next = 2
            else:
                w_mix_next = 3
                
            with gcs_state.registry_lock:
                w1_idle = 1 if any(info["node_type"] == "on_demand" and info["status"] == "IDLE" for info in gcs_state.worker_registry.values()) else 0
                w2_idle = 1 if any(info["node_type"] == "spot_a" and info["status"] == "IDLE" for info in gcs_state.worker_registry.values()) else 0
                w3_idle = 1 if any(info["node_type"] == "spot_b" and info["status"] == "IDLE" for info in gcs_state.worker_registry.values()) else 0
            a_mix_next = (w1_idle * 1) + (w2_idle * 2) + (w3_idle * 4)
            
            u_sla_next = 0
            peek_task_next = None
            with gcs_state.queue_lock:
                for t in gcs_state.task_queue:
                    deps_met = True
                    for dep in t.get("dependencies", []):
                        if not gcs_state.completed_tasks_cache.get(dep, False):
                            deps_met = False
                            break
                    if deps_met:
                        peek_task_next = t
                        break
            if peek_task_next:
                time_left_next = peek_task_next["deadline"] - time.time()
                if time_left_next <= 30.0:
                    u_sla_next = 1
                    
            b_avail_next = 0 if gcs_state.virtual_budget < 0.7 else 1
            next_state = (w_mix_next, a_mix_next, u_sla_next, b_avail_next)
            
            agent.update_q_value(state, action, reward, next_state)
            agent.save_q_table()
            
            dashboard.log_event(f"[Q-Learning Update] State={state} | Action={action} | Reward={reward:.4f} | NextState={next_state} | Epsilon={agent.epsilon:.4f}")
            dashboard.log_event(f"[Q-Learning Update] 잔여 가상 예산: ${gcs_state.virtual_budget:.4f}달러")
        else:
            dashboard.log_event(f"[Resource Spend] [Mode: {gcs_state.SCHEDULER_MODE}] 비용 차감: ${task_cost:.4f} | 잔여 예산: ${gcs_state.virtual_budget:.4f}")
        
        # 자동 실험 데이터 누적 기록 (Benchmark Logger)
        log_benchmark_metric(
            scheduler_mode=gcs_state.SCHEDULER_MODE,
            task_id=task_id,
            model_type=model_type,
            status="SUCCESS" if success else "FAILED",
            execution_time=execution_time,
            cost=task_cost,
            delay=delay_time,
            virtual_budget=gcs_state.virtual_budget
        )
        
        # 실패 시 복구 재삽입
        if not success:
            last_epoch = 0
            checkpoint_file = None
            for ep in range(epochs, 0, -1):
                chk_path = f"data/checkpoint_{task_id}_epoch_{ep}.pt"
                if os.path.exists(chk_path):
                    last_epoch = ep
                    checkpoint_file = chk_path
                    break
            
            if last_epoch > 0 and last_epoch < epochs:
                remaining_epochs = epochs - last_epoch
                dashboard.log_event(f"[장애 복구] 작업 {task_id} 중단 감지 -> {last_epoch} Epoch 가중치를 기반으로 이어서 학습 복구(남은 {remaining_epochs} Epochs) 대기 큐 재할당.")
                task["dataset_path"] = checkpoint_file
                task["epochs"] = remaining_epochs
            else:
                dashboard.log_event(f"[장애 복구] 작업 {task_id} 장애 유실 감지 -> 복구를 위해 대기 큐 재할당 (처음부터 재학습).")
                
            with gcs_state.queue_lock:
                gcs_state.task_queue.insert(0, task)
            gcs_state.save_gcs_state()
