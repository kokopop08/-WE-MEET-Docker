# ==============================================================================
# WE-MEET: 공통 환경 설정 관리 파일 (common/config.py)
# ==============================================================================

# gRPC 기본 포트 설정
DEFAULT_HEAD_PORT = 50051 #Header 노드 기본 포트
DEFAULT_WORKER_PORT = 50052 #worker 노드 기본 포트

# 생존 신호 (Heartbeat) 송신 주기 (초 단위)
DEFAULT_HEARTBEAT_INTERVAL = 5.0
