"""WE-MEET: Head Node gRPC 서버 메인 컨트롤러 (head/head.py)
"""

import os
import sys

# 프로젝트 루트 디렉토리를 path에 추가하여 wemeet 패키지 임포트 지원
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

import grpc
from concurrent import futures
import time
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



from wemeet.transport.proto import babyray_pb2
from wemeet.transport.proto import babyray_pb2_grpc
from wemeet.config.settings import DEFAULT_HEAD_PORT

# 모듈화된 구성요소 임포트
import wemeet.cluster.gcs_state as state # 전역 상태 관리
import wemeet.cluster.manager as cluster_manager # docker/cGroup 관련 함수 모음
import wemeet.scheduling.daemon as scheduler# Q-Learning 기반 스케줄러
import wemeet.observability.dashboard as dashboard # 대시보드 HTTP 서버


# gRPC 서비서·상태 스냅샷은 head_service 로 분리(부팅 오케스트레이션만 이 파일에 남김).
from wemeet.transport.head_service import BabyRayHeadServicer, get_dashboard_data
def serve():
    """
    Head Node 메인 서비스 데몬을 구동합니다.
    좀비 컨테이너 소거 비동기 스레드, 대시보드 웹 서버, gRPC 서버, Q-Learning 백그라운드 스케줄러 루프를 초기화합니다.
    """
    # 0. 도커가 재부팅되거나 새로 기동될 때 이전 벤치마크 태스크 잔재 및 통계를 완전 삭제하기 위해 백업 파일 소거
    from wemeet.config import paths
    gcs_backup_path = paths.gcs_state_path()
    if os.path.exists(gcs_backup_path):
        try:
            os.remove(gcs_backup_path)
            print("[Head Boot] 이전 실행의 GCS 영속 상태 백업(gcs_state.json)을 강제 소거하고 초기 통계로 세팅했습니다.")
        except Exception as e:
            print(f"[Head Boot 경고] 백업 파일 소거 실패: {e}")

    # GCS 상태 파일 복구 (파일 소거 후이므로 초기 상태로 세팅됨)
    state.load_gcs_state()

    # 0.1. 잔존 좀비 컨테이너 동기 청소 (부팅 전 이전 라이프사이클의 잔재 완전 소거를 통한 정합성 확보)
    cluster_manager.cleanup_zombie_containers()
    
    # 0.2. 스팟 강제 회수(Eviction) 모니터링 백그라운드 루프 작동
    cluster_manager.start_spot_eviction_loop() # cluster_manger.py 참고
    
    # 0.5. 실시간 GUI 모니터링 대시보드 서버 기동 (8080 포트)
    dashboard.start_dashboard_server(port=8080, data_callback=get_dashboard_data)
    
    # 환경 변수에서 헤드 노드 포트를 읽어오고, 설정되지 않았을 경우 기본값 사용
    port = os.environ.get("HEAD_PORT", str(DEFAULT_HEAD_PORT)) # 기본값: 8000
    
    # gRPC 서버 기동 (동시 접속 스레드풀 설정)
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=20))
    # 최대 20개의 worker 생성
    
    babyray_pb2_grpc.add_BabyRayServiceServicer_to_server(BabyRayHeadServicer(), server)
    server.add_insecure_port(f"0.0.0.0:{port}")
    server.start()
    print(f"=== [Head] Baby Ray 마스터 Node gRPC 서버 기동 완료 (포트: {port}) ===")
    
    # 백그라운드 Q-Learning 의사결정 스케줄러 스레드 기동
    scheduler_thread = threading.Thread(target=scheduler.scheduler_loop, daemon=True)
    scheduler_thread.start()
    
    is_shutting_down = False

    def handle_shutdown(signum, frame):
        nonlocal is_shutting_down
        if is_shutting_down:
            return
        is_shutting_down = True
        print(f"\n[Head] 종료 시그널 수신 (Signal: {signum}). Graceful Shutdown 시작...")
        try:
            server.stop(0)
        except Exception:
            pass
        
        # 공유 볼륨 내 임시/최종 가중치 파일 정리 (파일 누수 차단)
        try:
            from wemeet.config import paths
            print("[Head] 공유 볼륨 내 임시/최종 가중치 파일(.pt)들을 정리합니다...")
            leftover_files = paths.glob_data("checkpoint_*.pt") + paths.glob_data("final_*.pt")
            for f_path in leftover_files:
                if os.path.exists(f_path):
                    os.remove(f_path)
            print(f"[Head] 총 {len(leftover_files)}개의 가중치 파일이 정리되었습니다.")
        except Exception as e:
            print(f"[Head] 공유 볼륨 정리 중 오류 발생: {e}")

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

    # 메인 루프: 평상시엔 상주하며 대기한다. 단, EXPERIMENT_AUTOEXIT 가 켜진 무인 실험에서는
    # 스케줄러가 예산 소진으로 정상 종료(gcs_state.SCHEDULER_COMPLETED)하면 head 도 graceful 종료한다.
    try:
        while True:
            time.sleep(1.0)
            if getattr(state, "EXPERIMENT_AUTOEXIT", False) and getattr(state, "SCHEDULER_COMPLETED", False):
                print("[Head] 실험 완료(예산 소진) 감지 -> 자동 종료(EXPERIMENT_AUTOEXIT)를 수행합니다.")
                handle_shutdown("AUTOEXIT", None)
    except KeyboardInterrupt:
        handle_shutdown(signal.SIGINT, None)

if __name__ == '__main__':
    serve()
