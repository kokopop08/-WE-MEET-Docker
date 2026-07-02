# ==============================================================================
# WE-MEET: Head Node gRPC 서버 메인 컨트롤러 (head/head.py)
# ==============================================================================

import grpc
from concurrent import futures # 비동기 작업 / 스테드 풀 관리
import time
import os
import sys # 모듈 검색 경로 조작 sys
import psutil
import threading
import signal

# 표준 출력 버퍼 비우기 (Flush) 설정
import builtins
_original_print = builtins.print
def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    _original_print(*args, **kwargs)
builtins.print = print

# 실행 시 프로젝트 루트 디렉토리 및 현재 디렉토리를 sys.path에 추가하여 패키지들을 정상적으로 찾을 수 있도록 설정합니다.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

# 루트에 있는 proto나 common 패키지를 어디서든 임포트할 수 있게 함

from proto import babyray_pb2
from proto import babyray_pb2_grpc
from common.config import DEFAULT_HEAD_PORT

# 모듈화된 구성요소 임포트
import head.state as state # 전역 상태 관리
import head.cluster_manager as cluster_manager # docker/cGroup 관련 함수 모음
import head.scheduler.core as scheduler# Q-Learning 기반 스케줄러
import head.dashboard.server as dashboard # 대시보드 HTTP 서버

class BabyRayHeadServicer(babyray_pb2_grpc.BabyRayServiceServicer):
    """
    Baby Ray Head Node의 gRPC 서비스 처리를 전담하는 서비서 클래스입니다.
    GCS(Global Control Store) 역할을 하는 state.worker_registry를 갱신 및 조회합니다.
    """
    def RegisterWorker(self, request, context):
        """
        워커 노드를 클러스터 및 GCS에 신규 등록합니다.

        Args:
            request (RegisterRequest): 워커 ID, 노드 타입 및 포트 번호가 담긴 요청 메시지.
            context (grpc.ServicerContext): gRPC 서비스 컨텍스트.

        Returns:
            RegisterResponse: 등록 성공 여부 및 결과 메시지.
        """
        #  gRPC나 분산 시스템에서 현재 실행 중인 컨텍스트의 상대방(peer) 정보를 가져오는 명령어 (IPv4 주소 확보)
        peer = context.peer() 
        
        # gRPC peer IP 주소 파싱 (IPv4 및 IPv6 호환)

        if peer.startswith("ipv4:"):
            ip = peer.split(":")[1]
        # "ipv4:192.168.0.10:50051" → "192.168.0.10" (IPv4)

        elif peer.startswith("ipv6:"):
            last_colon = peer.rfind(":")
            ip = peer[5:last_colon]
            ip = ip.replace("%5B", "").replace("%5D", "").replace("[", "").replace("]", "")
        # "ipv6:[2001:db8::1]:50051" → "2001:db8::1" (IPv6)

        else:
            ip = "127.0.0.1"
        # 둘 다 아닐 경우에는 로컬 IP로 간주
            
        if ip == "::1":
            ip = "127.0.0.1"

        # [아키텍처 디자인 선택 (Trade-off)]
        # 분산 시스템에서는 원래 고가용성(HA)과 상태 영속성을 보장하기 위해 분산 합의 저장소(예: etcd, ZooKeeper)나
        # 외부 Redis 등을 사용하는 것이 정석입니다. 다만, 본 프로젝트는 Docker 기반 경량 분산 런타임(Baby Ray)을 지향하므로
        # 배포 편의성과 오버헤드 최소화를 위해 단일 Head 노드 내 인메모리 딕셔너리 + Lock 동기화 방식을 채택하였습니다.
        with state.registry_lock:
            # 1. 중복 ID 검증 -> 이름이 겹쳐서 나느 충돌을 회피
            if request.worker_id in state.worker_registry:
                dashboard.log_event(f"[Head Registry 경고] 워커 등록 실패 (중복 ID 감지): ID='{request.worker_id}'")
                return babyray_pb2.RegisterResponse(
                    success=False,
                    message=f"Registration failed. Worker ID '{request.worker_id}' is already registered."
                )

            # 2. 신규 등록 진행
            state.worker_registry[request.worker_id] = {
                "node_type": request.node_type.lower(),
                "ip": ip,
                "port": request.port,
                "last_heartbeat": time.time(),
                "cpu": 0.0,
                "mem": 0.0,
                "status": "IDLE"
            }
            # HTTP 서버에 출력
            dashboard.log_event(f"[Head Registry] 워커 신규 등록: ID='{request.worker_id}' | 주소: {ip}:{request.port} | 타입: {request.node_type}")
        
        # 워커에게 성공 응답 전송 (grpc)
        return babyray_pb2.RegisterResponse(
            success=True, 
            message=f"Worker '{request.worker_id}' registered successfully on Head GCS."
        )

    def DeregisterWorker(self, request, context):
        """
        워커 노드가 퇴장할 때 GCS의 레지스트리에서 해당 워커 정보를 삭제합니다.

        Args:
            request (DeregisterRequest): 퇴장할 워커 식별자가 포함된 요청 메시지.
            context (grpc.ServicerContext): gRPC 서비스 컨텍스트.

        Returns:
            DeregisterResponse: 해제 성공 여부 및 결과 메시지.
        """
        # state.worker_registry에서 해당 워커 정보를 삭제 (lock 사용)
        with state.registry_lock:
            # worker_id가 레지스트리에 있는지 확인
            if request.worker_id in state.worker_registry:
                # 삭제 (퇴장 처리) - 인메모리 캐시 제거
                del state.worker_registry[request.worker_id]
                print(f"[Head Registry] 워커 정상 퇴장: ID='{request.worker_id}'")
                return babyray_pb2.DeregisterResponse(success=True, message="Deregistered.")
            
            # worker_id가 없으면
            return babyray_pb2.DeregisterResponse(success=False, message="Worker not found.")

    def SendHeartbeat(self, request, context):
        """
        워커로부터 실시간 자원 상태 및 생존 신고(Heartbeat)를 받아 GCS를 업데이트합니다.

        Args:
            request (HeartbeatRequest): 워커 ID 및 CPU, 메모리 자원 사용량 요청 메시지.
            context (grpc.ServicerContext): gRPC 서비스 컨텍스트.

        Returns:
            HeartbeatResponse: 수신 응답(Ack) 메시지.
        """
        # 워커 ID를 기반으로 컨테이너 이름 생성 (worker-1은 babyray-on-demand로 매핑)
        if request.worker_id == "worker-1":
            container_name = "babyray-on-demand"
        else:
            container_name = f"babyray-{request.worker_id}"

        # 컨테이너의 실제 CPU 및 메모리 사용량 조회
        real_cpu, real_mem = cluster_manager.get_container_metrics(container_name)
        
        with state.registry_lock:
            if request.worker_id in state.worker_registry:
                # 워커의 마지막 하트비트 시간 갱신
                state.worker_registry[request.worker_id]["last_heartbeat"] = time.time()
                # SDK 실시간 자원량 값 주입 (실패 시 하트비트 전송자가 송신한 더미 값 반영) - 기본적인 값은 0.0 / OOM이 trigger 되면 99.9%의 형태
                state.worker_registry[request.worker_id]["cpu"] = real_cpu if real_cpu > 0 else request.cpu_utilization
                state.worker_registry[request.worker_id]["mem"] = real_mem if real_mem > 0 else request.memory_utilization
                
                # 수신된 메트릭 로그 출력 (콘솔에만 출력하여 대시보드 로그 flooding 방지)
                print(f"[Head GCS] Heartbeat 수신 | ID: '{request.worker_id}' | CPU: {state.worker_registry[request.worker_id]['cpu']}%, Mem: {state.worker_registry[request.worker_id]['mem']}%")
                
        return babyray_pb2.HeartbeatResponse(ack=True)


def get_dashboard_data():
    """
    대시보드 HTTP API 조회를 위해 GCS 상태 데이터 스냅샷을 딕셔너리로 반환합니다.

    Returns:
        dict: 가상 예산, 워커 목록, 대기열, 호스트 CPU/메모리, GPU 가용 VRAM 정보가 포함된 딕셔너리.
    """
    with state.registry_lock:
        workers = {wid: info.copy() for wid, info in state.worker_registry.items()}
    with state.queue_lock:
        queue = [t.copy() for t in state.task_queue]

    # 완료/실패 태스크 통계 계산
    total_completed = sum(1 for status in state.task_status.values() if status in ["SUCCESS", "COMPLETED"])
    total_failed = sum(1 for status in state.task_status.values() if status == "FAILED")

    # Q-Learning Agent의 훈련 파라미터 획득
    from head.scheduler.utils import agent
    q_epsilon = getattr(agent, "epsilon", 0.0)

    # 대시보드 웹 API가 JSON 포맷 등으로 파싱하기 편하도록 최종 마스터 데이터 구조 구축
    return {
        "virtual_budget": state.virtual_budget,
        "scheduler_mode": state.SCHEDULER_MODE,
        "workers": workers,
        "queue": queue,
        "total_completed": total_completed,
        "total_failed": total_failed,
        "q_epsilon": q_epsilon,
        "host_cpu": psutil.cpu_percent(),
        "host_mem": psutil.virtual_memory().percent,
        "gpu_free_vram": cluster_manager.get_gpu_free_memory(),
        "conclusions": state.latest_conclusions
    }


def serve():
    """
    Head Node 메인 서비스 데몬을 구동합니다.
    좀비 컨테이너 소거 비동기 스레드, 대시보드 웹 서버, gRPC 서버, Q-Learning 백그라운드 스케줄러 루프를 초기화합니다.
    """
    # 0. 잔존 좀비 컨테이너 동기 청소 (부팅 전 이전 라이프사이클의 잔재 완전 소거를 통한 정합성 확보)
    cluster_manager.cleanup_zombie_containers()
    
    # 0.1. 스팟 강제 회수(Eviction) 모니터링 백그라운드 루프 작동
    cluster_manager.start_spot_eviction_loop() # cluster_manger.py 참고
    
    # 0.5. 실시간 GUI 모니터링 대시보드 서버 기동 (8080 포트)
    dashboard.start_dashboard_server(port=8080, data_callback=get_dashboard_data)
    
    # 환경 변수에서 헤드 노드 포트를 읽어오고, 설정되지 않았을 경우 기본값 사용
    port = os.environ.get("HEAD_PORT", str(DEFAULT_HEAD_PORT)) # 기본값: 8000
    
    # gRPC 서버 기동 (동시 접속 스레드풀 설정)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))
    # 최대 20개의 worker 생성
    
    babyray_pb2_grpc.add_BabyRayServiceServicer_to_server(BabyRayHeadServicer(), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    print(f"=== [Head] Baby Ray 마스터 Node gRPC 서버 기동 완료 (포트: {port}) ===")
    
    # 백그라운드 Q-Learning 의사결정 스케줄러 스레드 기동
    scheduler_thread = threading.Thread(target=scheduler.scheduler_loop, daemon=True)
    scheduler_thread.start()
    
    def handle_shutdown(signum, frame):
        print(f"\n[Head] 종료 시그널 수신 (Signal: {signum}). Graceful Shutdown 시작...")
        try:
            server.stop(0)
        except Exception:
            pass
        try:
            print("[Head] 기동 중인 모든 동적 스팟 워커 컨테이너들을 일괄 청소합니다...")
            cluster_manager.cleanup_zombie_containers()
        except Exception as e:
            print(f"[Head] 동적 컨테이너 소거 실패: {e}")
        print("[Head] Graceful Shutdown 완료. 프로세스를 안전하게 종료합니다.")
        sys.exit(0)

    # SIGINT(Ctrl+C) 및 SIGTERM(도커 정지) 등록
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    try:
        while True:
            time.sleep(86400)
    except KeyboardInterrupt:
        handle_shutdown(signal.SIGINT, None)

if __name__ == '__main__':
    serve()
