"""실시간 모니터링 대시보드 HTTP 서버 (wemeet/observability/dashboard.py).

``web/`` 정적 파일(index.html·style.css·app.js)을 서빙하고, ``/api/status`` 로 GCS 상태
스냅샷+이벤트 로그를 JSON 제공하며, ``/api/reset`` 으로 GCS 상태를 초기화한다.

이벤트 로그 채널 자체는 [wemeet.observability.event_log] 이 소유한다. 하위호환을 위해
``log_event``/``event_logs``/``event_lock`` 을 여기서 재-export 한다(기존 ``dashboard.log_event``
호출부가 그대로 동작). 신규 코드는 event_log 모듈을 직접 참조하는 것을 권장한다.
"""
import os
import http.server
import json
import threading

# 로그 채널은 event_log 모듈이 소유. 하위호환 재-export.
from wemeet.observability.event_log import log_event, event_logs, event_lock  # noqa: F401

# 대시보드 상태 데이터 획득을 위한 콜백 함수 (transport/head.py 에서 바인딩)
_data_callback = None


class DashboardHTTPHandler(http.server.BaseHTTPRequestHandler):
    """대시보드 정적 파일 서빙 + 상태/리셋 API 라우팅 핸들러."""

    def log_message(self, format, *args):
        # HTTP 요청 로그로 콘솔이 어지럽혀지지 않도록 비활성화
        pass

    def serve_static_file(self, filename, content_type):
        """``web/`` 폴더의 정적 파일을 no-cache 헤더와 함께 서빙한다.

        Args:
            filename (str): ``web/`` 하위 파일명.
            content_type (str): 응답 Content-Type 헤더값.
        """
        try:
            # 이 모듈 위치 기준 web/ 폴더 내 파일 반환
            current_dir = os.path.dirname(os.path.abspath(__file__))
            file_path = os.path.join(current_dir, "web", filename)

            if os.path.exists(file_path):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                with open(file_path, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Static File Not Found")
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"Internal Server Error: {str(e)}".encode("utf-8"))

    def do_GET(self):
        """GET 라우팅: ``/api/status``·``/api/reset``·정적 파일(``/``,``/style.css``,``/app.js``)."""
        global _data_callback

        if self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            # 콜백을 통해 실시간 데이터 획득
            data = {}
            if _data_callback:
                try:
                    data = _data_callback()
                except Exception as e:
                    data = {"error": f"Failed to fetch data: {str(e)}"}

            with event_lock:
                data["logs"] = list(event_logs)

            self.wfile.write(json.dumps(data).encode("utf-8"))
            return

        elif self.path == "/api/reset":
            import wemeet.cluster.gcs_state as state

            with state.registry_lock:
                with state.queue_lock:
                    state.task_queue.clear()
                    state.task_status.clear()
                    state.completed_tasks_cache.clear()
                    state.latest_conclusions.clear()
                    state.virtual_budget = state.INITIAL_VIRTUAL_BUDGET
                    state.task_counter = 0

            state.save_gcs_state()
            log_event("[Dashboard GCS] 사용자의 요청에 의해 GCS 클러스터 상태가 초기화되었습니다.")

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "message": "GCS state reset success."}).encode("utf-8"))
            return

        elif self.path in ["/", "/index.html"]:
            self.serve_static_file("index.html", "text/html; charset=utf-8")
            return

        elif self.path == "/style.css":
            self.serve_static_file("style.css", "text/css; charset=utf-8")
            return

        elif self.path == "/app.js":
            self.serve_static_file("app.js", "application/javascript; charset=utf-8")
            return

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")


def start_dashboard_server(port=8080, data_callback=None):
    """대시보드 HTTP 서버를 백그라운드 데몬 스레드로 기동한다.

    Args:
        port (int): 바인드할 포트(기본 8080).
        data_callback (callable | None): ``/api/status`` 응답에 병합할 상태 dict 반환 콜백.
    """
    global _data_callback
    _data_callback = data_callback

    def run_server():
        server_address = ("", port)
        try:
            httpd = http.server.HTTPServer(server_address, DashboardHTTPHandler)
            log_event(f"=== [Dashboard] 실시간 GUI 모니터링 대시보드 활성화 (http://localhost:{port}) ===")
            httpd.serve_forever()
        except Exception as e:
            log_event(f"[Dashboard 에러] 대시보드 서버 실행 중 오류 발생: {e}")

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()
