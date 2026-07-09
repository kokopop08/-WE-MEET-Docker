import os
import sys

# 프로젝트 루트 디렉토리를 path에 추가하여 wemeet 패키지 임포트 지원
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

import grpc
import socket
import psutil
import time
import argparse
import threading
from concurrent import futures #thread pool을 만들기 위한 모듈


from wemeet.transport.proto import babyray_pb2
from wemeet.transport.proto import babyray_pb2_grpc
from wemeet.config.settings import DEFAULT_HEARTBEAT_INTERVAL # 하트비트 전송 주기 - 가져옴 (파일에서 미리 정의)

# 분리된 GPU 시뮬레이터 모듈에서 실행기를 가져옴
from wemeet.workload.runner import PyTorchTaskRunner

# 표준 출력 버퍼 비우기 (Flush) 설정
import builtins
_original_print = builtins.print
def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    _original_print(*args, **kwargs)
builtins.print = print


# --- 1. Worker gRPC 서비스 서버 구현 ---

# gRPC 서비서는 worker_service 로 분리(부팅/하트비트만 이 파일).
from wemeet.transport.worker_service import BabyRayWorkerServicer
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
            
            # 가상 OOM 장애 모사 상태 체크 (oom_simulated 는 runner 모듈 전역, 동일 프로세스 공유)
            import wemeet.workload.runner as gpu_simulator
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