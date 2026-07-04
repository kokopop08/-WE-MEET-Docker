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
from head.scheduler.utils import agent, run_task_on_worker
from head.q_learning.scheduler import run_qlearning_scheduler_step
from head.scheduler.utils import agent, run_task_on_worker

def get_next_runnable_task():
    """
    태스크 큐에서 실행 가능한(DAG 의존성이 충족된) 첫 번째 태스크를 꺼내어 반환합니다.
    """
    with gcs_state.queue_lock:
        for i, task in enumerate(gcs_state.task_queue):
            deps_met = True
            for dep in task.get("dependencies", []):
                if not gcs_state.completed_tasks_cache.get(dep, False):
                    deps_met = False
                    break
            if deps_met:
                return gcs_state.task_queue.pop(i)
    return None

def get_current_spot_scale():
    """GCS 워커 레지스트리 내 spot_a 노드의 현재 대수를 카운트합니다."""
    with gcs_state.registry_lock:
        return sum(1 for info in gcs_state.worker_registry.values() if info["node_type"] == "spot_a")

def check_and_cleanup_dead_workers():
    """GCS 레지스트리와 Docker 호스트 상의 DEAD 워커 노드를 감시하고 강제 제거(회수)합니다."""
    current_time = time.time()
    dead_workers = []
    with gcs_state.registry_lock:
        for wid, info in list(gcs_state.worker_registry.items()):
            if current_time - info["last_heartbeat"] > 3.0:
                dead_workers.append(wid)
        for wid in dead_workers:
            dashboard.log_event(f"[Scheduler GCS] [DEAD 노드 감지] {wid} 노드가 오프라인 처리되었습니다.")
            del gcs_state.worker_registry[wid]

    for wid in dead_workers:
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
    
    # 버스티(Bursty) 태스크 유입 패턴 시뮬레이션
    # 8%의 확률로 '태스크 폭풍(Burst)' 발생: 5~8개의 태스크가 한번에 유입
    # 92%의 확률로는 5%의 매우 낮은 확률로만 단일 태스크 유입
    is_burst = random.random() < 0.08
    is_normal = not is_burst and (random.random() < 0.05)
    
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

# --- 5. 백그라운드 스케줄러 핵심 루프 ---

def scheduler_loop():
    """
    [백그라운드 Q-Learning 의사결정 스케줄러 핵심 루프]
    1초 주기로 돌면서 DEAD 노드를 검출 및 회수하고, 주기적인 가상 태스크를 생성하며,
    Q-Learning 정책(Epsilon-Greedy 및 Action Masking)에 따라 작업을 가용 워커에 다중 분배(Multi-Dispatch)합니다.
    """
    dashboard.log_event("[Scheduler] Q-Learning 비용/SLA 인지형 의사결정 엔진 가동 성공.")
    
    model_types = ["CNN", "RNN", "LSTM"]
    
    # 최대 동적 워커 스케일 상한선 (Spot-A/B 혼합 최대 7대 제한)
    MAX_SPOT_SCALE = 7
    
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
