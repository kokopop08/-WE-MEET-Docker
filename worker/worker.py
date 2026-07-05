import grpc
import sys
import os
import socket
import psutil
import time
import argparse
import threading
from concurrent import futures #thread pool을 만들기 위한 모듈

# 실행 시 프로젝트 루트 디렉토리 및 현재 디렉토리를 sys.path에 추가하여 패키지들을 정상적으로 찾을 수 있도록 설정합니다.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
sys.path.append(os.path.abspath(os.path.dirname(__file__)))

from proto import babyray_pb2
from proto import babyray_pb2_grpc
from common.config import DEFAULT_HEARTBEAT_INTERVAL # 하트비트 전송 주기 - 가져옴 (파일에서 미리 정의)

# 분리된 GPU 시뮬레이터 모듈에서 실행기를 가져옴
from gpu_simulator import PyTorchTaskRunner

# 표준 출력 버퍼 비우기 (Flush) 설정
import builtins
_original_print = builtins.print
def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    _original_print(*args, **kwargs)
builtins.print = print


# --- 1. Worker gRPC 서비스 서버 구현 ---
class BabyRayWorkerServicer(babyray_pb2_grpc.BabyRayServiceServicer):
    """
    Baby Ray Worker Node의 gRPC 서비스 처리를 전담하는 서비서 클래스입니다.
    Head로부터의 작업 할당 및 상태 확인 요청에 대응합니다.
    """
    def __init__(self, worker_type):
        """
        BabyRayWorkerServicer 인스턴스를 초기화합니다.

        Args:
            worker_type (str): 워커 노드 유형 ("on_demand" / "spot_a").
        """
        self.worker_type = worker_type # worker의 종류
        self.current_task_id = None #작업 ID를 저장
        self.runner = None # 실제 수행할 객체 - PyTorchTaskRunner.py에 있는 클래스
        self.lock = threading.Lock() # 여러 스레드가 동시에 변수를 건드리지 못하게 막는 race condition 방지

    def AssignTask(self, request, context):
        """
        새로운 AI 연산 작업을 할당받아 백그라운드 스레드에서 비동기로 실행합니다.

        Args:
            request (TaskAssignment): 할당받을 작업 정보가 담긴 요청 메시지.
            context (grpc.ServicerContext): gRPC 서비스 컨텍스트.

        Returns:
            TaskResult: 작업 할당 결과 메시지 (RUNNING 또는 FAILED).
        """
        with self.lock: # 안에 있는 critical section에 mutual exclusion 보장
            # 1. 중복 검사: 이미 작업 ID가 있고, 그 작업이 'RUNNING' 상태라면
            if self.current_task_id is not None and self.runner.status == "RUNNING":
                print(f"[Worker gRPC] 작업 거절: {request.task_id} (이유: 다른 작업 실행 중)")
                # TaskResult Return
                return babyray_pb2.TaskResult(
                    task_id=request.task_id,
                    status="FAILED",
                    execution_time=0.0,
                    message="Another task is already running on this worker."
                )
            
            # 2. 신규 작업 생성: 새로운 작업 ID를 저장합니다.
            self.current_task_id = request.task_id
            # # 전달받은 옵션으로 딥러닝 구동기(Runner) 객체를 생성합니다. -> 함수에서 정의 받은 옵션으로 만들기
            self.runner = PyTorchTaskRunner(
                task_id=request.task_id,
                model_type=request.model_type,
                epochs=request.epochs,
                worker_type=self.worker_type,
                dataset_path=request.dataset_path
            )
            
            # 3. 백그라운드 실행: 새로운 스레드를 만들어 runner.run 함수를 백그라운드에서 실행시킵니다.
            # daemon=True 설정: 메인 프로그램(worker.py)이 종료되면 이 스레드도 자동으로 함께 종료됩니다.
            threading.Thread(target=self.runner.run, daemon=True).start()
            
            # 정상적으로 작업이 생성되었을 때 TaskResult Return
            print(f"[Worker gRPC] 작업 접수 승인: {request.task_id}")
            return babyray_pb2.TaskResult(
                task_id=request.task_id,
                status="RUNNING",
                execution_time=0.0,
                message="Task assigned successfully, executing in background."
            )

    # 작업 상태 조회
    def GetTaskStatus(self, request, context):
        """
        현재 수행 중인 AI 연산 작업의 상태 및 진행 로그를 반환합니다.

        Args:
            request (TaskStatusRequest): 확인할 작업 ID 정보.
            context (grpc.ServicerContext): gRPC 서비스 컨텍스트.

        Returns:
            TaskStatusResponse: 작업 진행 상태, 진행률 및 학습 로그 문자열.
        """
        with self.lock: #mutual exclusion 보장
            # 현재 실행 중인 작업이 없거나, 작업 ID가 요청과 다르면
            if self.runner is None or self.runner.task_id != request.task_id:
                return babyray_pb2.TaskStatusResponse(
                    status="NOT_FOUND",
                    progress=0.0,
                    logs="No such task found on this worker."
                )
            #제대로 된 요청이라면 현재 작업의 상태, 진행률, 로그를 모아서 반환
            return babyray_pb2.TaskStatusResponse(
                status=self.runner.status,
                progress=self.runner.progress,
                logs="\n".join(self.runner.logs)
            )
            
    # request - 클라이언트가 보낸 자원 크기
    def ResizeResources(self, request, context):
        """
        워커 노드의 cGroup 자원 격리 한도를 동적으로 조정합니다 (현재 스펙 정의용).

        Args:
            request (ResizeRequest): 변경할 CPU 코어 수 및 메모리 용량.
            context (grpc.ServicerContext): gRPC 서비스 컨텍스트.

        Returns:
            ResizeResponse: 조정 성공 여부 메시지.
        """
        print(f"[Worker gRPC] 자원 크기 조절 요청 수신: CPU={request.cpu_cores} Cores, Mem={request.memory_bytes} Bytes")
        # response - 헤드에게 보내는 답변
        return babyray_pb2.ResizeResponse(
            success=True,
            message=f"Configured worker cGroups: CPU={request.cpu_cores}, Mem={request.memory_bytes}"
        )


# --- 2. 하트비트 송신 클라이언트 루프 (Head로 전송) ---

def heartbeat_sender_loop(worker_id, node_type, port, head_host, head_port):
    """
    주기적으로 Head Node로 생존 신고 및 자원 상태(CPU/메모리) 메트릭을 송신하는 루프입니다.

    Args:
        worker_id (str): 현재 워커 노드 고유 ID.
        node_type (str): 현재 워커 노드 유형 ("on_demand" / "spot_a").
        port (int): 현재 워커 노드의 수신 대기 gRPC 포트 번호.
        head_host (str): Head Node의 호스트명/IP 주소.
        head_port (int): Head Node의 gRPC 서버 포트 번호.
    """
    time.sleep(1.0) # Worker 자체 gRPC 서버가 부팅될 때까지 1초 대기
    
    head_address = f"{head_host}:{head_port}" # Head 노드의 주소(IP:Port)를 만듭니다.
    print(f"[Heartbeat] Head 서버 연결 시도: {head_address}...")
    
    # 1. Head 서버에 워커 등록 요청
    registered = False # 등록 여부
    channel = None
    stub = None
    while not registered: # 등록이 될 때까지 반복
        try:
            channel = grpc.insecure_channel(head_address) # 연결 채널 생성
            stub = babyray_pb2_grpc.BabyRayServiceStub(channel) # stub 객체 생성 - grpc 통신 프로토컬 저장
            
            # Head 노드의 RegisterWorker 함수 호출 -> 내 정보에 등록
            response = stub.RegisterWorker(babyray_pb2.RegisterRequest(
                worker_id=worker_id,
                node_type=node_type,
                port=port
            ))
            if response.success:
                print(f"[Heartbeat] Head 서버 등록 완료: {response.message}")
                registered = True
            else:
                print(f"[Heartbeat] 등록 거절됨. 3초 후 재시도...")
                if channel is not None:
                    channel.close()
                time.sleep(3)
        # Worker는 살아있으나, Worker와 Head 사이의 네트워크 회선이 끊어졌거나 Head 서버 자체가 크래시(Crash)되어 다운된 상황이다.
        except grpc.RpcError:
            print(f"[Heartbeat] Head 서버 연결 지연. 3초 후 재시도...")
            if channel is not None:
                channel.close()
            time.sleep(3) # 3초 후 재시도 
            
    # 2. 주기적 생존 신고 및 상태 리포트
    # 첫 호출 전 CPU 메트릭 캘리브레이션을 진행합니다.
    psutil.cpu_percent(interval=None)
    
    while True:
        try:
            # 1. 실제 CPU 사용률 수집 (이전 호출 이후의 점유비)
            cpu_util = psutil.cpu_percent(interval=None)
            
            # 2. 실제 메모리 사용률 수집 (cgroup 메모리 제한 대비 사용량 우선 조회)
            mem_util = 0.0
            try:
                # cgroup v1 메모리 조회
                with open("/sys/fs/cgroup/memory/memory.usage_in_bytes", "r") as f:
                    usage = int(f.read().strip())
                with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", "r") as f:
                    limit = int(f.read().strip())
                mem_util = (usage / limit) * 100.0 if limit > 0 else psutil.virtual_memory().percent
            except Exception:
                try:
                    # cgroup v2 메모리 조회
                    with open("/sys/fs/cgroup/memory.current", "r") as f:
                        usage = int(f.read().strip())
                    with open("/sys/fs/cgroup/memory.max", "r") as f:
                        limit_str = f.read().strip()
                        limit = int(limit_str) if limit_str != "max" else psutil.virtual_memory().total
                    mem_util = (usage / limit) * 100.0 if limit > 0 else psutil.virtual_memory().percent
                except Exception:
                    # Fallback: 호스트 기준 가상 메모리 사용률
                    mem_util = psutil.virtual_memory().percent
            
            # 가상 OOM 장애 모사 상태 체크
            import gpu_simulator
            if getattr(gpu_simulator, "oom_simulated", False):
                cpu_util = 1.5
                mem_util = 99.9

            # 실시간 자원 수치 송신
            stub.SendHeartbeat(babyray_pb2.HeartbeatRequest(
                worker_id=worker_id,
                cpu_utilization=round(cpu_util, 1),
                memory_utilization=round(mem_util, 1)
            ))
            # 콘솔에 전송 메트릭 출력
            print(f"[Heartbeat] 생존 신고 송신 -> CPU: {round(cpu_util, 1)}%, Mem: {round(mem_util, 1)}%")
            
        # Worker는 살아있으나, Worker와 Head 사이의 네트워크 회선이 끊어졌거나 Head 서버 자체가 크래시(Crash)되어 다운된 상황이다.
        except grpc.RpcError:
            print(f"[Heartbeat] 경고: 생존 신고 전송 실패 (Head 연결이 끊겼습니다)")
            
        time.sleep(DEFAULT_HEARTBEAT_INTERVAL)


# --- 3. Worker 메인 구동 루프 ---

def serve(worker_id, node_type, port, head_host, head_port):
    """
    Worker Node 메인 데몬 서버를 구동합니다.
    자체 gRPC 수신 대기 서버를 실행하고, Head Node에 주기적으로 하트비트를 송신하는 스레드를 가동합니다.

    Args:
        worker_id (str): 워커 고유 ID.
        node_type (str): 워커 유형 ("on_demand" / "spot_a").
        port (int): 워커가 gRPC 수신 대기할 포트 번호.
        head_host (str): Head Node의 호스트명/IP 주소.
        head_port (int): Head Node의 gRPC 수신 포트 번호.
    """
    # 1. Head의 명령을 수신받을 Worker 자체 gRPC 서버 실행
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=3)) #Head로 부터 요청이 병목이 생기지 않도록 스레드 3개を用意
    servicer = BabyRayWorkerServicer(worker_type=node_type) # Worker 서버 객체 생성
    babyray_pb2_grpc.add_BabyRayServiceServicer_to_server(servicer, server) # 서비서를 서버에 등록
    server.add_insecure_port(f"[::]:{port}") # 서버 포트 설정
    server.start() # 서버 시작
    print(f"=== [Worker] '{worker_id}' ({node_type}) gRPC 서버 활성화 (포트: {port}) ===")
    
    # 2. Head에 하트비트를 보내는 클라이언트 스레드 가동
    hb_thread = threading.Thread(
        target=heartbeat_sender_loop,
        args=(worker_id, node_type, port, head_host, head_port),
        daemon=True
    )
    hb_thread.start()
    
    #process가 코드가 끝까지 도달 했을 때 종료시키기 않기 위해서 sleep을 걸어 둠
    try:
        while True:
            time.sleep(86400) # 24시간 -> 리소스는 소모 안함

    # ctrl + C = 종료 gracefull shutdown
    except KeyboardInterrupt:
        print(f"\n[Worker] '{worker_id}' 종료 중...")
        try:
            # 종료 시 GCS 해제 요청
            channel = grpc.insecure_channel(f"{head_host}:{head_port}")
            stub = babyray_pb2_grpc.BabyRayServiceStub(channel)
            stub.DeregisterWorker(babyray_pb2.DeregisterRequest(worker_id=worker_id))
        except Exception:
            pass
        server.stop(0) # grpc 요청을 안기다리고 닫음


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="BabyRay Worker Node")
    parser.add_argument("--id", type=str, default="worker-01", help="Worker ID")
    parser.add_argument("--type", type=str, default="on_demand", help="Worker Type")
    parser.add_argument("--port", type=int, default=50052, help="Worker listening port")
    parser.add_argument("--head-host", type=str, default=os.environ.get("HEAD_HOST", "localhost"), help="Head node IP/Host")
    parser.add_argument("--head-port", type=int, default=int(os.environ.get("HEAD_PORT", 50051)), help="Head node port")
    
    args = parser.parse_args()
    # 순차 할당된 ID 자체가 고유하므로 접미사 생략
    unique_worker_id = args.id
    serve(unique_worker_id, args.type, args.port, args.head_host, args.head_port)
# python worker.py --id worker-02 --port 50053 --type spot -> serve 함수 구동