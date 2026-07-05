import time
import os
import sys
import random
import threading
import grpc

# 표준 출력 버퍼 비우기 (Flush) 설정
import builtins
_original_print = builtins.print
def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    _original_print(*args, **kwargs)
builtins.print = print

# 실행 시 프로젝트 루트 디렉토리를 sys.path에 추가 
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from proto import babyray_pb2
from proto import babyray_pb2_grpc
from head.q_learning.agent import QLearningAgent

# state 모듈을 gcs_state라는 별칭으로 임포트하여 로컬 변수 state와 충돌하지 않게 함
import head.state as gcs_state # gcs state (변수, 인메모리 캐시 모음), q-learning state와의 차별을 두기 위해서
import head.cluster_manager as cluster_manager
import head.dashboard.server as dashboard

from head.scheduler.static import run_static_scheduler_step
from head.scheduler.dynamic import run_dynamic_scheduler_step
from head.scheduler.q_learning import run_qlearning_scheduler_step
# Q-Learning 에이전트 및 연산 구동 공통 헬퍼 임포트 (task_executor에서 통합 로드)
from head.scheduler.task_executor import agent, run_task_on_worker


def get_next_runnable_task():
    """
    태스크 큐에서 실행 가능한(DAG 의존성이 충족된) 첫 번째 태스크를 꺼내어 반환합니다.
    """
    # 1. 큐 데이터 보호를 위한 Lock 획득 (state.py의 queue_lock 사용)
    with gcs_state.queue_lock:
        # 2. 전역 대기열(task_queue)의 모든 태스크를 순회 (인덱스와 함께 가져옴)
        for i, task in enumerate(gcs_state.task_queue):
            deps_met = True
            
            # 3. 현재 태스크가 의존하고 있는 부모 태스크 ID 목록(dependencies)을 확인
            for dep in task.get("dependencies", []):
                
                # 4. 부모 태스크가 아직 completed_tasks_cache에 등록되지 않았거나 완료되지 않았다면 실행 불가능으로 간주
                if not gcs_state.completed_tasks_cache.get(dep, False):
                    deps_met = False
                    break
                # 의존성 O = merge task
            # 5. 모든 부모 의존성이 해결된 경우, 큐에서 빼내어 반환
            if deps_met:
                return gcs_state.task_queue.pop(i)
    # 6. 실행 가능한 태스크가 하나도 없을 경우 None 반환
    return None

def get_current_spot_scale():
    """Docker 호스트에 실존하는 spot 컨테이너 대수를 직접 세어 정확한 스케일 대수를 반환합니다 (등록 지연으로 인한 초과 기동 방지)."""
    # 파이썬에서는 None을 비교할 때  == 보다 is 권장
    if gcs_state.DOCKER_CLIENT is None:
        with gcs_state.registry_lock:
            # 총 scale 수 = spot_a 수 + spot_b 수 counting
            return sum(1 for info in gcs_state.worker_registry.values() if info["node_type"] in ["spot_a", "spot_b"])
    # 오류 시에는 레지스트리 내의 값 사용
    try:
        containers = gcs_state.DOCKER_CLIENT.containers.list(all=True)
        count = 0
        for c in containers:
            c_name = c.name
            # babyray-worker-2- (spot_a), babyray-worker-3- (spot_b)
            if c_name.startswith("babyray-worker-2-") or c_name.startswith("babyray-worker-3-"):
                if c.status in ["running", "restarting", "created"]:
                    count += 1
        return count
    
    except Exception:
        with gcs_state.registry_lock:
            return sum(1 for info in gcs_state.worker_registry.values() if info["node_type"] in ["spot_a", "spot_b"])

def check_and_cleanup_dead_workers():
    """GCS 레지스트리와 Docker 호스트 상의 DEAD 워커 노드를 감시하고 강제 제거(회수)합니다."""
    current_time = time.time()
    dead_workers = []
    recovered_tasks = []

    with gcs_state.registry_lock:
        # list()를 사용하여 순회 중인 딕셔너리 항목을 안전하게 삭제 (DEAD 노드 식별 및 수집)
        for wid, info in list(gcs_state.worker_registry.items()):
            # worker-1 / on-demand가 회수 되지 않도록 함
            if wid == "worker-1" or info.get("node_type") == "on_demand":
                continue
            if current_time - info["last_heartbeat"] > 5.0:
                dead_workers.append(wid)

        # 수집된 DEAD 노드 처리
        for wid in dead_workers:
            # 로그 기록
            dashboard.log_event(f"[Scheduler GCS] [DEAD 노드 감지] {wid} 노드가 오프라인 처리되었습니다.")
            # 레지스트리에서 삭제
            del gcs_state.worker_registry[wid]
            
            # GCS Task Lineage DAG 조회 및 의존 유실 태스크 구조
            for sub_task_id, lineage_info in list(gcs_state.task_lineage.items()):
                if lineage_info["worker_id"] == wid and lineage_info["status"] == "RUNNING":
                    lineage_info["status"] = "FAILED"
                    
                    # 최신 체크포인트 탐색하여 이어서 학습 재개 연동
                    last_epoch = 0
                    checkpoint_file = None
                    epochs = lineage_info["epochs"]
                    for ep in range(epochs, 0, -1):
                        chk_path = f"data/checkpoint_{sub_task_id}_epoch_{ep}.pt"
                        if os.path.exists(chk_path):
                            last_epoch = ep
                            checkpoint_file = chk_path
                            break
                    
                    recovered_epochs = epochs
                    recovered_dataset = lineage_info["dataset_path"]
                    if last_epoch > 0 and last_epoch < epochs:
                        recovered_epochs = epochs - last_epoch
                        recovered_dataset = checkpoint_file
                        dashboard.log_event(f"[장애 복구] 유실된 subtask {sub_task_id} 중단 감지 -> {last_epoch} Epoch 가중치를 기반으로 이어서 학습 복구(남은 {recovered_epochs} Epochs) 대기 큐 재할당.")
                    else:
                        dashboard.log_event(f"[장애 복구] 유실된 subtask {sub_task_id} 장애 유실 감지 -> 복구를 위해 대기 큐 재할당 (처음부터 재학습).")

                    recovered_tasks.append({
                        "task_id": sub_task_id,
                        "model_type": lineage_info["model_type"],
                        "epochs": recovered_epochs,
                        "deadline": time.time() + 45.0,
                        "enqueue_time": time.time(),
                        "dataset_path": recovered_dataset,
                        "is_recovered_subtask": True
                    })

    if recovered_tasks:
        with gcs_state.queue_lock:
            for task in recovered_tasks:
                gcs_state.task_queue.insert(0, task)
                dashboard.log_event(f"[Lineage Recovery] !!! Cascaded Recovery 작동 !!! DEAD 워커에서 유실된 subtask '{task['task_id']}'를 대기열 0순위로 복구했습니다!")
        gcs_state.save_gcs_state()
            
        # Auto Scale-out 연동: 유실된 노드를 대체하기 위해 스팟 노드 증설 요청
        dashboard.log_event(f"[Lineage Recovery] 노드 이탈로 인한 대체 자원 Scale-Out 요청 트리거")
        cluster_manager.scale_out_worker("spot_a")

    for wid in dead_workers:
        if wid == "worker-1":
            continue
        if wid.startswith("worker-2-") or wid.startswith("worker-3-"):
            container_ref = f"babyray-{wid}"
            try:
                if gcs_state.DOCKER_CLIENT is not None:
                    container = gcs_state.DOCKER_CLIENT.containers.get(container_ref)
                    dashboard.log_event(f"[Docker SDK] DEAD 컨테이너 회수 시작: {container_ref}")
                    container.stop(timeout=2)
                    container.remove()
                    dashboard.log_event(f"[Docker SDK] DEAD 컨테이너 회수 성공: {container_ref}")
            except Exception as e:
                dashboard.log_event(f"[Docker SDK 경고] DEAD 컨테이너 {container_ref} 회수 실패: {e}")

def generate_mock_tasks():
    """시뮬레이터 부하 검증을 위해 주기적으로 랜덤 가상 태스크를 생성하여 큐에 적재합니다."""
    model_types = ["CNN", "RNN", "LSTM"]
    
    # 4%의 확률로 '태스크 폭풍(Burst)' 발생: 5~8개의 태스크가 한번에 유입
    # 96%의 확률로는 3%의 매우 낮은 확률로만 단일 태스크 유입
    is_burst = random.random() < 0.04
    is_normal = not is_burst and (random.random() < 0.03)
    
    if is_burst:
        num_new_tasks = random.randint(5, 8)
        dashboard.log_event(f"⚡ [Burst Traffic Alert] 태스크 폭발 유입 발생! (신규: {num_new_tasks}개)")
    elif is_normal:
        num_new_tasks = 1
    else:
        num_new_tasks = 0
        
    if num_new_tasks > 0:
        with gcs_state.queue_lock:
            if len(gcs_state.task_queue) < 25:  # 버스트 수용을 위해 최대 큐 크기 상향
                for _ in range(num_new_tasks):
                    gcs_state.task_counter += 1
                    task_id = f"task-{gcs_state.task_counter:04d}"
                    model = random.choice(model_types)
                    epochs = random.randint(12, 20)
                    timeout = random.randint(60, 100)
                    deadline = time.time() + timeout
                    gcs_state.task_queue.append({
                        "task_id": task_id,
                        "model_type": model,
                        "epochs": epochs,
                        "deadline": deadline,
                        "enqueue_time": time.time()
                    })
                    dashboard.log_event(f"[Task 유입] {task_id} ({model}, {epochs} Epochs) 큐 적재 완료. (마감기한: {timeout}초 후)")
        gcs_state.save_gcs_state()

# --- 5. 백그라운드 스케줄러 핵심 루프 ---

def scheduler_loop():
    """
    [백그라운드 Q-Learning 의사결정 스케줄러 핵심 루프]
    1초 주기로 돌면서 DEAD 노드를 검출 및 회수하고, 주기적인 가상 태스크를 생성하며,
    Q-Learning 정책(Epsilon-Greedy 및 Action Masking)에 따라 작업을 가용 워커에 다중 분배(Multi-Dispatch)합니다.
    """
    dashboard.log_event("[Scheduler] Q-Learning 비용/SLA 인지형 의사결정 엔진 가동 성공.")
    
    model_types = ["CNN", "RNN", "LSTM"]
    
    import head.resource_guard as resource_guard
    recommended_scale = resource_guard.get_recommended_max_spot_scale()
    MAX_SPOT_SCALE = max(5, recommended_scale)
    dashboard.log_event(f"[Scheduler] 호스트 물리 RAM 감지 기반 MAX_SPOT_SCALE 설정 완료: {MAX_SPOT_SCALE}대")
    
    # 초기 컨테이너 대수 세팅 (Compose 기본 스펙 기준)
    # docker-compose.yml에서 spot 워커(worker-2, 3)는 주석 처리되어 있으므로 초기 기동 대수는 0대입니다.
    current_worker_2_scale = 0
    
    # 타이머 초기화
    empty_queue_duration = 0.0
    scale_in_timer = 0.0
    
    while True:
        time.sleep(1.0)  # 1초 주기 의사결정 루프
        
        # --- 0. 가상 예산 실시간 차감 (노드 상시 구동 비용 청구) ---
        with gcs_state.registry_lock:
            for wid, info in gcs_state.worker_registry.items():
                node_type = info.get("node_type", "on_demand").lower()
                cost_profile = agent.nodes_config.get(node_type, {"cost_per_hour": 0.0})
                cost_per_hour = cost_profile.get("cost_per_hour", 0.0)
                gcs_state.virtual_budget -= (cost_per_hour / 3600.0)
                
        # --- 1. DEAD 노드 헬스체크 및 격리 제거 ---
        check_and_cleanup_dead_workers()
 
        # --- 2. 주기적 랜덤 가상 태스크 자동 생성 및 큐 투입 (시뮬레이터 구동용) ---
        generate_mock_tasks()
 
        # --- 3. 각 모드별 의사결정 서브 모듈 위임 ---
        #  함수 자체를 변수처럼 다른 함수로 넘겨주는 '콜백(Callback)' 
        #  '의존성 주입(Dependency Injection)' 아키텍처
        if gcs_state.SCHEDULER_MODE == "static":
            scale_in_timer = run_static_scheduler_step(
                MAX_SPOT_SCALE,
                scale_in_timer,
                run_task_on_worker,
                get_next_runnable_task,
                get_current_spot_scale
            )
        elif gcs_state.SCHEDULER_MODE == "dynamic":
            scale_in_timer = run_dynamic_scheduler_step(
                MAX_SPOT_SCALE,
                scale_in_timer,
                run_task_on_worker,
                get_next_runnable_task,
                get_current_spot_scale
            )
        elif gcs_state.SCHEDULER_MODE == "q_learning":
            empty_queue_duration = run_qlearning_scheduler_step(
                MAX_SPOT_SCALE,
                empty_queue_duration,
                agent,
                run_task_on_worker,
                get_next_runnable_task,
                get_current_spot_scale
            )
