"""전역 이벤트 로그 채널 (wemeet/observability/event_log.py).

그동안 ``log_event`` 가 대시보드 모듈 안에 정의되어 있어, 스케줄러·클러스터 등
관측(observability)과 무관한 모듈들이 로그 한 줄을 남기려고 대시보드 모듈에 역결합되어
있었다. 이 모듈이 로그 채널의 유일 소유자이며, 대시보드는 여기서 읽어 웹으로 노출한다.

인메모리 링버퍼(최신 100건)를 유지하고 콘솔에도 출력한다. 스레드 안전.
"""

import sys
import time
import threading

#: 최근 이벤트 로그(최신 100건 유지). 대시보드 ``/api/status`` 가 이 리스트를 읽어 노출.
event_logs = []
#: ``event_logs`` 동시 접근 보호용 락.
event_lock = threading.Lock()

_MAX_LOGS = 100


def log_event(message):
    """이벤트 메시지를 콘솔에 출력하고 인메모리 로그 버퍼에 적재한다.

    Args:
        message (str): 기록할 로그 메시지(타임스탬프는 자동 부착).
    """
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    full_message = f"{timestamp} {message}"

    # 1) 표준 출력
    print(message)
    sys.stdout.flush()

    # 2) 인메모리 로그 저장 (최신 _MAX_LOGS건 유지)
    with event_lock:
        event_logs.append(full_message)
        if len(event_logs) > _MAX_LOGS:
            event_logs.pop(0)
