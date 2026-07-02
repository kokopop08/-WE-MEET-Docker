import threading

# --- 전역 리소스 상태 및 GCS (Global Control Store) 정의 ---

# 워커 관리용 인메모리 GCS 레지스트리
# worker_id -> { "node_type": str, "ip": str, "port": int, "last_heartbeat": float, "cpu": float, "mem": float, "status": str }
worker_registry = {} # 인메모리 캐시
"""dict: 활성 워커들의 상세 상태 정보를 저장하는 글로벌 Control Store 맵."""

registry_lock = threading.Lock() # 인메모리 캐시 접근을 위한 락
"""threading.Lock: worker_registry의 스레드 안전성을 확보하기 위한 뮤텍스 락."""

# 가상 태스크 대기열 (Task Queue)
task_queue = [] # 스케줄링을 대기하는 태스크 리스트
"""list: 스케줄링을 대기하는 태스크 리스트."""

queue_lock = threading.Lock() # 큐에 접근하는 경우 경쟁 상태를 방지하기 위한 락
"""threading.Lock: task_queue 접근을 제어하기 위한 뮤텍스 락."""

# 태스크 상태 및 캐시 관리 GCS 레지스트리
task_status = {}
"""dict: 태스크 ID별 현재 상태 ("PENDING", "RUNNING", "SUCCESS", "FAILED") 관리 GCS 레포지토리."""

completed_tasks_cache = {}
"""dict: 완료된 태스크 ID별 결과물 캐시 여부 (True/False) 관리 레포지토리."""

# Task Lineage DAG 관리 레지스트리 (장애 자가 복구용)
task_lineage = {}
"""dict: 태스크 의존성 계보 및 할당 워커 매핑 관리 레포지토리."""

# 전역 가상 자산 관리 변수
virtual_budget = 10.0  # 초기 예산 $10.0달러 (현실적인 AWS 요율과 밸런싱을 맞추기 위해 10.0달러로 조정)
"""float: 현재 사용 가능한 가상 잔여 예산 ($)."""

task_counter = 0        # 고유한 TASK ID 생성을 위한 카운터 변수
"""int: 고유 태스크 식별 번호 발급을 위한 전역 카운터."""

# 최종 훈련/추론 예측 결론을 대시보드에 뿌려주기 위한 저장소
latest_conclusions = []
"""list: 태스크별 최종 FedAvg 병합 및 추론 결론 텍스트의 누적 레포지토리."""

# 스케줄러 구동 모드
SCHEDULER_MODE = "dynamic"
"""str: 현재 활성화된 스케줄러 구동 모드 ("static", "dynamic", "q_learning")."""

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

