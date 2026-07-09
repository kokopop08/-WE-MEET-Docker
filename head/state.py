import threading

# --- 전역 리소스 상태 및 GCS (Global Control Store) 정의 ---

# 워커 관리용 인메모리 GCS 레지스트리
# worker_id -> { "node_type": str, "ip": str, "port": int, "last_heartbeat": float, "cpu": float, "mem": float, "status": str }
worker_registry = {} # 인메모리 캐시
"""dict: 활성 워커들의 상세 상태 정보를 저장하는 글로벌 Control Store 맵."""

registry_lock = threading.RLock() # 인메모리 캐시 접근을 위한 락
"""threading.RLock: worker_registry의 스레드 안전성을 확보하기 위한 뮤텍스 락."""

# 가상 태스크 대기열 (Task Queue)
task_queue = [] # 스케줄링을 대기하는 태스크 리스트
"""list: 스케줄링을 대기하는 태스크 리스트."""

queue_lock = threading.RLock() # 큐에 접근하는 경우 경쟁 상태를 방지하기 위한 락
"""threading.RLock: task_queue 접근을 제어하기 위한 뮤텍스 락."""

# 태스크 상태 및 캐시 관리 GCS 레지스트리
task_status = {}
"""dict: 태스크 ID별 현재 상태 ("PENDING", "RUNNING", "SUCCESS", "FAILED") 관리 GCS 레포지토리."""

completed_tasks_cache = {}
"""dict: 완료된 태스크 ID별 결과물 캐시 여부 (True/False) 관리 레포지토리."""

# Task Lineage DAG 관리 레지스트리 (장애 자가 복구용)
task_lineage = {}
"""dict: 태스크 의존성 계보 및 할당 워커 매핑 관리 레포지토리."""

# 전역 가상 자산 관리 변수
# 버스트 벤치마크 시나리오(명세서 Scenario 1)에서 예산 축이 실제로 물게 하려는 시나리오 파라미터.
# 짧은 런에서도 자원을 남발하는 Static이 조기 파산하도록 초기 예산을 낮게 설정한다. (튜닝 포인트)
INITIAL_VIRTUAL_BUDGET = 1.5
virtual_budget = INITIAL_VIRTUAL_BUDGET  # 초기 예산 ($)
"""float: 현재 사용 가능한 가상 잔여 예산 ($)."""

task_counter = 0        # 고유한 TASK ID 생성을 위한 카운터 변수
"""int: 고유 태스크 식별 번호 발급을 위한 전역 카운터."""

# 최종 훈련/추론 예측 결론을 대시보드에 뿌려주기 위한 저장소
latest_conclusions = []
"""list: 태스크별 최종 FedAvg 병합 및 추론 결론 텍스트의 누적 레포지토리."""

# 스케줄러 구동 모드
SCHEDULER_MODE = "q_learning"
"""str: 현재 활성화된 스케줄러 구동 모드 ("static", "dynamic", "q_learning")."""

# Q-Learning 실행 모드 설정 (True: 온라인 추가 학습 진행, False: 사전 학습본으로 고속 추론 및 배정만 수행)
Q_LEARNING_TRAINING_MODE = True
"""bool: Q-Learning의 온라인 학습 및 테이블 실시간 영속화 여부를 결정하는 모드 플래그."""

# Docker SDK 클라이언트 공통 객체
DOCKER_CLIENT = None # Docker API 서버와 통신, 컨테이너 관리
"""docker.DockerClient: 도커 컨테이너를 직접 조작하기 위한 SDK 클라이언트 인스턴스."""

try:
    import docker
    DOCKER_CLIENT = docker.from_env()
    print("[Docker SDK] 호스트 도커 데몬 연결 성공.")
except Exception as e:
    DOCKER_CLIENT = None
    print(f"[Docker SDK 경고] 도커 데몬 연결 실패 (예외 안전 모드 가동): {e}")

# --- GCS 상태 영속 저장 및 복구 함수 ---
import json
import os

STATE_FILE = "data/gcs_state.json"

def save_gcs_state():
    """GCS 상태 변수들을 data/gcs_state.json 파일로 영속 보존합니다."""
    with registry_lock:
        with queue_lock:
            state_data = {
                "task_queue": task_queue,
                "task_status": task_status,
                "completed_tasks_cache": completed_tasks_cache,
                "task_lineage": task_lineage,
                "virtual_budget": virtual_budget,
                "task_counter": task_counter,
                "latest_conclusions": latest_conclusions
            }
            try:
                os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
                with open(STATE_FILE, "w", encoding="utf-8") as f:
                    json.dump(state_data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"[GCS State 경고] 상태 영속화 저장 실패: {e}")

def load_gcs_state():
    """GCS 상태 파일이 존재하면 읽어와서 메모리 상태를 복구합니다."""
    global task_queue, task_status, completed_tasks_cache, task_lineage, virtual_budget, task_counter, latest_conclusions
    if not os.path.exists(STATE_FILE):
        return False
    
    with registry_lock:
        with queue_lock:
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state_data = json.load(f)
                
                # 값 복원 및 마감 기한(Deadline) 현재 시간 기준으로 보정 (시프트)
                import time
                current_time = time.time()
                loaded_queue = state_data.get("task_queue", [])
                for task in loaded_queue:
                    try:
                        if "deadline" in task and "enqueue_time" in task:
                            duration = task["deadline"] - task["enqueue_time"]
                            # 비정상 값이거나 음수일 경우 기본 60초 보정
                            if duration <= 0:
                                duration = 60.0
                            task["deadline"] = current_time + duration
                            task["enqueue_time"] = current_time
                        else:
                            task["deadline"] = current_time + 60.0
                            task["enqueue_time"] = current_time
                    except Exception:
                        task["deadline"] = current_time + 60.0
                        task["enqueue_time"] = current_time
                
                task_queue.clear()
                task_queue.extend(loaded_queue)
                
                task_status.clear()
                task_status.update(state_data.get("task_status", {}))
                
                completed_tasks_cache.clear()
                completed_tasks_cache.update(state_data.get("completed_tasks_cache", {}))
                
                task_lineage.clear()
                task_lineage.update(state_data.get("task_lineage", {}))
                
                virtual_budget = state_data.get("virtual_budget", virtual_budget)
                task_counter = state_data.get("task_counter", task_counter)
                
                latest_conclusions.clear()
                latest_conclusions.extend(state_data.get("latest_conclusions", []))
                
                print(f"[GCS State] 상태 복구 완료. 대기 큐: {len(task_queue)}개, 캐시: {len(completed_tasks_cache)}개, Lineage: {len(task_lineage)}개, 예산: ${virtual_budget:.4f}달러")
                return True
            except Exception as e:
                print(f"[GCS State 경고] 상태 복구 실패 (파일을 로드하지 않고 빈 상태로 기동): {e}")
                return False

