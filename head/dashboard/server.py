import http.server
import json
import threading
import time
import os
import sys

# 전역 로그 저장소 및 락
event_logs = []
event_lock = threading.Lock()

# 대시보드 상태 데이터 획득을 위한 콜백 함수 (head.py에서 바인딩)
_data_callback = None

def log_event(message):
    """
    콘솔 출력과 동시에 대시보드 실시간 로그에 이벤트를 적재합니다.
    """
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    full_message = f"{timestamp} {message}"
    
    # 1. 표준 출력
    print(message)
    sys.stdout.flush()

    # 2. 인메모리 로그 저장 (최신 100개 유지)
    with event_lock:
        event_logs.append(full_message)
        if len(event_logs) > 100:
            event_logs.pop(0)

class DashboardHTTPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # HTTP 요청 로그로 콘솔이 어지럽혀지지 않도록 비활성화
        pass

    def serve_static_file(self, filename, content_type):
        try:
            # server.py 위치 기준 web/ 폴더 내 파일 반환
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
            import head.state as state
            
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
    """
    백그라운드 스레드에서 대시보드 HTTP 서버를 기동합니다.
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

# 기존 레거시 HTML 상수는 무시 처리합니다.
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BabyRay Cluster Dashboard</title>
    <!-- Google Fonts -->
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;600;800&family=Fira+Code:wght@400;500&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-primary: #090d16;
            --bg-glass: rgba(17, 24, 39, 0.65);
            --bg-glass-hover: rgba(30, 41, 59, 0.75);
            --border-glass: rgba(255, 255, 255, 0.08);
            --accent-purple: #8b5cf6;
            --accent-blue: #3b82f6;
            --accent-green: #10b981;
            --accent-red: #ef4444;
            --text-main: #f8fafc;
            --text-secondary: #94a3b8;
            --shadow-glow: 0 8px 32px 0 rgba(139, 92, 246, 0.15);
        }

        * {
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }

        body {
            font-family: 'Inter', sans-serif;
            background-color: var(--bg-primary);
            background-image: 
                radial-gradient(at 10% 20%, rgba(59, 130, 246, 0.1) 0px, transparent 50%),
                radial-gradient(at 90% 80%, rgba(139, 92, 246, 0.1) 0px, transparent 50%);
            background-attachment: fixed;
            color: var(--text-main);
            min-height: 100vh;
            padding: 2rem;
            line-height: 1.5;
        }

        .container {
            max-width: 1440px;
            margin: 0 auto;
        }

        /* --- Header Section --- */
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 2rem;
            padding: 1.5rem;
            background: var(--bg-glass);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid var(--border-glass);
            border-radius: 16px;
            box-shadow: var(--shadow-glow);
        }

        .logo-section h1 {
            font-family: 'Outfit', sans-serif;
            font-size: 1.75rem;
            font-weight: 800;
            background: linear-gradient(135deg, #a78bfa 0%, #3b82f6 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            letter-spacing: -0.025em;
        }

        .logo-section p {
            font-size: 0.85rem;
            color: var(--accent-blue);
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            margin-top: 0.25rem;
        }

        .system-status {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            background: rgba(16, 185, 129, 0.1);
            border: 1px solid rgba(16, 185, 129, 0.2);
            padding: 0.5rem 1rem;
            border-radius: 9999px;
            font-size: 0.875rem;
            color: var(--accent-green);
            font-weight: 600;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            background-color: var(--accent-green);
            border-radius: 50%;
            position: relative;
            animation: pulse-green 1.5s infinite;
        }

        @keyframes pulse-green {
            0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
            70% { transform: scale(1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
            100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
        }

        /* --- Dashboard Grid Layout --- */
        .dashboard-grid {
            display: grid;
            grid-template-columns: 1fr 1fr 1fr;
            gap: 1.5rem;
            margin-bottom: 1.5rem;
        }

        /* Row 1 cards */
        .budget-card {
            grid-column: span 1;
            background: linear-gradient(135deg, rgba(139, 92, 246, 0.15) 0%, rgba(59, 130, 246, 0.05) 100%);
            backdrop-filter: blur(12px);
            border: 1px solid rgba(139, 92, 246, 0.25);
        }

        .metrics-card {
            grid-column: span 2;
        }

        .card {
            background: var(--bg-glass);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid var(--border-glass);
            border-radius: 16px;
            padding: 1.5rem;
            transition: all 0.3s ease;
        }

        .card:hover {
            background: var(--bg-glass-hover);
            border-color: rgba(255, 255, 255, 0.12);
            transform: translateY(-2px);
        }

        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.25rem;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            padding-bottom: 0.75rem;
        }

        .card-title {
            font-family: 'Outfit', sans-serif;
            font-size: 1.1rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .budget-value {
            font-family: 'Outfit', sans-serif;
            font-size: 3.25rem;
            font-weight: 800;
            color: #ffffff;
            text-shadow: 0 0 20px rgba(139, 92, 246, 0.3);
            margin-top: 0.5rem;
            display: flex;
            align-items: baseline;
        }

        .budget-value span {
            font-size: 1.5rem;
            color: var(--text-secondary);
            margin-left: 0.25rem;
            font-weight: 500;
        }

        /* Host resource bars */
        .resource-row {
            display: grid;
            grid-template-columns: repeat(3, 1fr);
            gap: 1.5rem;
        }

        .resource-item {
            display: flex;
            flex-direction: column;
        }

        .resource-label {
            font-size: 0.85rem;
            color: var(--text-secondary);
            margin-bottom: 0.5rem;
            display: flex;
            justify-content: space-between;
        }

        .resource-bar-bg {
            height: 12px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 9999px;
            overflow: hidden;
            border: 1px solid rgba(255, 255, 255, 0.03);
            position: relative;
        }

        .resource-bar-fill {
            height: 100%;
            background: linear-gradient(90deg, var(--accent-blue) 0%, var(--accent-purple) 100%);
            border-radius: 9999px;
            width: 0%;
            transition: width 1s ease-in-out;
        }

        /* --- Section Columns --- */
        .main-section-grid {
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 1.5rem;
            margin-bottom: 1.5rem;
        }

        /* --- Worker Container Panel --- */
        .workers-panel {
            min-height: 350px;
        }

        .workers-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
            gap: 1rem;
            margin-top: 0.5rem;
        }

        .worker-card {
            background: rgba(15, 23, 42, 0.4);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 12px;
            padding: 1.25rem;
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
            transition: all 0.2s ease;
        }

        .worker-card:hover {
            border-color: rgba(139, 92, 246, 0.3);
            background: rgba(15, 23, 42, 0.6);
        }

        .worker-title-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .worker-id {
            font-family: 'Outfit', sans-serif;
            font-weight: 600;
            font-size: 1rem;
        }

        .worker-badge-type {
            font-size: 0.75rem;
            padding: 0.15rem 0.5rem;
            border-radius: 4px;
            font-weight: 600;
            text-transform: uppercase;
        }

        .type-on-demand {
            background: rgba(59, 130, 246, 0.15);
            color: #60a5fa;
            border: 1px solid rgba(59, 130, 246, 0.3);
        }

        .type-spot {
            background: rgba(139, 92, 246, 0.15);
            color: #c084fc;
            border: 1px solid rgba(139, 92, 246, 0.3);
        }

        .worker-status {
            font-size: 0.8rem;
            font-weight: 700;
            padding: 0.25rem 0.6rem;
            border-radius: 9999px;
            text-align: center;
        }

        .status-idle {
            background: rgba(16, 185, 129, 0.15);
            color: var(--accent-green);
            border: 1px solid rgba(16, 185, 129, 0.3);
        }

        .status-busy {
            background: rgba(59, 130, 246, 0.15);
            color: var(--accent-blue);
            border: 1px solid rgba(59, 130, 246, 0.3);
            position: relative;
            overflow: hidden;
        }

        .status-busy::after {
            content: '';
            position: absolute;
            top: 0; left: -100%; width: 100%; height: 100%;
            background: linear-gradient(90deg, transparent, rgba(59, 130, 246, 0.2), transparent);
            animation: shimmer 1.5s infinite;
        }

        @keyframes shimmer {
            100% { left: 100%; }
        }

        .worker-stats {
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
        }

        .worker-stat-bar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            font-size: 0.75rem;
            color: var(--text-secondary);
        }

        .worker-mini-bar {
            width: 100px;
            height: 6px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 9999px;
            overflow: hidden;
            margin-left: 0.5rem;
        }

        .worker-mini-fill {
            height: 100%;
            background: var(--accent-blue);
            border-radius: 9999px;
            width: 0%;
            transition: width 0.5s ease;
        }

        .worker-heartbeat {
            font-size: 0.75rem;
            color: var(--text-secondary);
            border-top: 1px solid rgba(255, 255, 255, 0.03);
            padding-top: 0.5rem;
            display: flex;
            justify-content: space-between;
        }

        /* --- Task Queue Panel --- */
        .queue-panel {
            min-height: 350px;
            display: flex;
            flex-direction: column;
        }

        .queue-list {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
            overflow-y: auto;
            max-height: 350px;
            padding-right: 0.25rem;
        }

        /* Custom Scrollbar */
        ::-webkit-scrollbar {
            width: 6px;
        }
        ::-webkit-scrollbar-track {
            background: rgba(255, 255, 255, 0.02);
            border-radius: 9999px;
        }
        ::-webkit-scrollbar-thumb {
            background: rgba(255, 255, 255, 0.1);
            border-radius: 9999px;
        }
        ::-webkit-scrollbar-thumb:hover {
            background: rgba(255, 255, 255, 0.2);
        }

        .task-card {
            background: rgba(15, 23, 42, 0.4);
            border: 1px solid rgba(255, 255, 255, 0.05);
            border-radius: 12px;
            padding: 1rem;
            display: flex;
            flex-direction: column;
            gap: 0.5rem;
            position: relative;
        }

        .task-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .task-id {
            font-family: 'Fira Code', monospace;
            font-weight: 600;
            font-size: 0.9rem;
            color: var(--accent-purple);
        }

        .task-model-badge {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: #ffffff;
            font-size: 0.75rem;
            padding: 0.1rem 0.4rem;
            border-radius: 4px;
            font-weight: 500;
        }

        .task-details {
            font-size: 0.75rem;
            color: var(--text-secondary);
            display: flex;
            justify-content: space-between;
        }

        .task-deadline-bar {
            height: 4px;
            background: rgba(255, 255, 255, 0.05);
            border-radius: 9999px;
            overflow: hidden;
            margin-top: 0.25rem;
        }

        .task-deadline-fill {
            height: 100%;
            background: var(--accent-green);
            width: 100%;
            transition: width 1s linear, background-color 0.5s ease;
        }

        /* --- Real-Time Console Panel --- */
        .console-panel {
            grid-column: span 3;
            background: rgba(9, 13, 22, 0.85);
            border: 1px solid rgba(139, 92, 246, 0.15);
        }

        .console-window {
            background: rgba(0, 0, 0, 0.4);
            border: 1px solid rgba(255, 255, 255, 0.03);
            border-radius: 8px;
            height: 250px;
            font-family: 'Fira Code', monospace;
            font-size: 0.85rem;
            padding: 1rem;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 0.35rem;
            scroll-behavior: smooth;
        }

        .console-line {
            line-height: 1.4;
            white-space: pre-wrap;
        }

        /* No data placeholders */
        .empty-placeholder {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            color: var(--text-secondary);
            font-size: 0.9rem;
            height: 150px;
            text-align: center;
            border: 1px dashed rgba(255, 255, 255, 0.05);
            border-radius: 12px;
            padding: 1rem;
        }

        .empty-placeholder svg {
            width: 32px;
            height: 32px;
            color: rgba(255, 255, 255, 0.1);
            margin-bottom: 0.75rem;
        }

        /* Responsive */
        @media (max-width: 1024px) {
            .dashboard-grid {
                grid-template-columns: 1fr;
            }
            .metrics-card {
                grid-column: span 1;
            }
            .main-section-grid {
                grid-template-columns: 1fr;
            }
            .console-panel {
                grid-column: span 1;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <header>
            <div class="logo-section">
                <h1>BabyRay Master Controller</h1>
                <p>WE-MEET Q-Learning Cluster Monitoring Dashboard</p>
            </div>
            <div class="system-status">
                <div class="status-dot"></div>
                <span id="status-text">GCS ONLINE</span>
            </div>
        </header>

        <!-- Top Row Dashboard Cards -->
        <div class="dashboard-grid">
            <!-- Budget Card -->
            <div class="card budget-card">
                <div class="card-header">
                    <span class="card-title">
                        <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>
                        가상 자산 잔여 예산
                    </span>
                </div>
                <div class="budget-value" id="budget-val">
                    $100.0000<span>USD</span>
                </div>
            </div>

            <!-- Host Resource Card -->
            <div class="card metrics-card">
                <div class="card-header">
                    <span class="card-title">
                        <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 002 2h2a2 2 0 002-2z"/></svg>
                        호스트 마스터 물리 자원 모니터링
                    </span>
                </div>
                <div class="resource-row">
                    <div class="resource-item">
                        <div class="resource-label">
                            <span>Host CPU</span>
                            <span id="host-cpu-val">0%</span>
                        </div>
                        <div class="resource-bar-bg">
                            <div class="resource-bar-fill" id="host-cpu-bar"></div>
                        </div>
                    </div>
                    <div class="resource-item">
                        <div class="resource-label">
                            <span>Host RAM</span>
                            <span id="host-mem-val">0%</span>
                        </div>
                        <div class="resource-bar-bg">
                            <div class="resource-bar-fill" id="host-mem-bar"></div>
                        </div>
                    </div>
                    <div class="resource-item">
                        <div class="resource-label">
                            <span>Host GPU VRAM (가용)</span>
                            <span id="gpu-vram-val">N/A</span>
                        </div>
                        <div class="resource-bar-bg">
                            <div class="resource-bar-fill" id="gpu-vram-bar" style="background: linear-gradient(90deg, #10b981 0%, #3b82f6 100%);"></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Workers and Queue Grid -->
        <div class="main-section-grid">
            <!-- Left: Active Workers -->
            <div class="card workers-panel">
                <div class="card-header">
                    <span class="card-title">
                        <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10"/></svg>
                        액티브 워커 노드 상태 정보
                    </span>
                    <span style="font-size: 0.8rem; font-weight: 600; color: var(--accent-blue);" id="worker-count">총 0대 가동 중</span>
                </div>
                <div class="workers-grid" id="workers-container">
                    <!-- Dynamic Content -->
                    <div class="empty-placeholder">
                        <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636"/></svg>
                        <span>활성화된 워커 노드가 없습니다.<br>GCS가 하트비트 연결을 대기 중입니다.</span>
                    </div>
                </div>
            </div>

            <!-- Right: Task Queue -->
            <div class="card queue-panel">
                <div class="card-header">
                    <span class="card-title">
                        <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 10h16M4 14h16M4 18h16"/></svg>
                        가상 태스크 대기열 (Queue)
                    </span>
                    <span style="font-size: 0.8rem; font-weight: 600; color: var(--accent-purple);" id="queue-count">대기 0개</span>
                </div>
                <div class="queue-list" id="queue-container">
                    <!-- Dynamic Content -->
                    <div class="empty-placeholder">
                        <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/></svg>
                        <span>현재 대기 중인 태스크가 없습니다.<br>스케줄러가 부하 상황을 대기하고 있습니다.</span>
                    </div>
                </div>
            </div>
        </div>

        <!-- Terminal Console Logs -->
        <div class="card console-panel">
            <div class="card-header">
                <span class="card-title">
                    <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"/></svg>
                    실시간 클러스터 오케스트레이션 콘솔 이벤트 로그
                </span>
                <span style="font-size: 0.85rem; font-family: monospace; color: var(--text-secondary);">Real-Time Event Stream</span>
            </div>
            <div class="console-window" id="console-logs">
                <div class="console-line" style="color: var(--text-secondary);">[System] 대시보드 콘솔 채널 연결 수립 완료. 이벤트를 기다립니다...</div>
            </div>
        </div>
    </div>

    <!-- Javascript Dashboard Logic -->
    <script>
        const API_URL = "/api/status";
        let lastLogCount = 0;

        function colorizeLog(msg) {
            if (msg.includes("[DEAD") || msg.includes("경고") || msg.includes("실패") || msg.includes("Error") || msg.includes("에러") || msg.includes("장애")) {
                return `<span style="color: #ef4444; font-weight: 600;">${msg}</span>`;
            }
            if (msg.includes("완료 성공") || msg.includes("성공") || msg.includes("SUCCESS") || msg.includes("가동") || msg.includes("연결 성공")) {
                return `<span style="color: #10b981; font-weight: 500;">${msg}</span>`;
            }
            if (msg.includes("[Scheduler Action]") || msg.includes("SCALE_OUT") || msg.includes("트리거") || msg.includes("스케일아웃") || msg.includes("감축")) {
                return `<span style="color: #8b5cf6; font-weight: 500;">${msg}</span>`;
            }
            if (msg.includes("Heartbeat 수신") || msg.includes("하트비트")) {
                return `<span style="color: #64748b; font-size: 0.8rem;">${msg}</span>`;
            }
            return `<span style="color: #e2e8f0;">${msg}</span>`;
        }

        async function updateDashboard() {
            try {
                const response = await fetch(API_URL);
                if (!response.ok) throw new Error("HTTP error");
                
                const data = await response.json();
                
                // Status online text
                document.getElementById("status-text").innerText = "GCS ONLINE";
                document.getElementById("status-text").parentElement.style.background = "rgba(16, 185, 129, 0.1)";
                document.getElementById("status-text").parentElement.style.color = "var(--accent-green)";
                document.getElementById("status-text").parentElement.style.borderColor = "rgba(16, 185, 129, 0.2)";

                // 1. Budget
                if (data.virtual_budget !== undefined) {
                    document.getElementById("budget-val").innerHTML = `$${data.virtual_budget.toFixed(4)}<span>USD</span>`;
                }

                // 2. Host Resources
                if (data.host_cpu !== undefined) {
                    document.getElementById("host-cpu-val").innerText = `${data.host_cpu.toFixed(1)}%`;
                    document.getElementById("host-cpu-bar").style.width = `${data.host_cpu}%`;
                }
                if (data.host_mem !== undefined) {
                    document.getElementById("host-mem-val").innerText = `${data.host_mem.toFixed(1)}%`;
                    document.getElementById("host-mem-bar").style.width = `${data.host_mem}%`;
                }
                if (data.gpu_free_vram !== undefined) {
                    const freeVram = data.gpu_free_vram;
                    if (freeVram === -1) {
                        document.getElementById("gpu-vram-val").innerText = "N/A (No GPU)";
                        document.getElementById("gpu-vram-bar").style.width = "0%";
                    } else {
                        document.getElementById("gpu-vram-val").innerText = `${freeVram} MiB`;
                        // Assume standard 8GB (8192MB) for full bar reference
                        const pct = Math.min(100, (freeVram / 8192) * 100);
                        document.getElementById("gpu-vram-bar").style.width = `${pct}%`;
                    }
                }

                // 3. Workers
                const workersContainer = document.getElementById("workers-container");
                const workersList = Object.entries(data.workers || {});
                document.getElementById("worker-count").innerText = `총 ${workersList.length}대 가동 중`;

                if (workersList.length === 0) {
                    workersContainer.innerHTML = `
                        <div class="empty-placeholder" style="grid-column: span 3;">
                            <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636"/></svg>
                            <span>활성화된 워커 노드가 없습니다.<br>GCS가 하트비트 연결을 대기 중입니다.</span>
                        </div>
                    `;
                } else {
                    let html = "";
                    const now = Date.now() / 1000;
                    
                    workersList.forEach(([wid, info]) => {
                        const statusClass = info.status === "IDLE" ? "status-idle" : "status-busy";
                        const typeClass = info.node_type === "on_demand" ? "type-on-demand" : "type-spot";
                        const typeName = info.node_type === "on_demand" ? "On-Demand" : "Spot-A";
                        
                        const cpuPercent = info.cpu || 0;
                        const memPercent = info.mem || 0;
                        const hbAge = Math.max(0, now - info.last_heartbeat).toFixed(1);
                        
                        // Heartbeat warning state if older than 5.0 seconds
                        const isWarning = hbAge > 5.0;
                        const hbStyle = isWarning ? "color: #ef4444; font-weight: bold;" : "color: var(--text-secondary);";
                        const warningOverlay = isWarning ? `border-color: rgba(239, 68, 68, 0.4); box-shadow: 0 0 10px rgba(239, 68, 68, 0.1);` : "";

                        html += `
                            <div class="worker-card" style="${warningOverlay}">
                                <div class="worker-title-row">
                                    <span class="worker-id">${wid}</span>
                                    <span class="worker-badge-type ${typeClass}">${typeName}</span>
                                </div>
                                
                                <div class="worker-status ${statusClass}">
                                    ${info.status} ${isWarning ? '(지연)' : ''}
                                </div>

                                <div class="worker-stats">
                                    <div class="worker-stat-bar">
                                        <span>CPU 사용률</span>
                                        <div style="display:flex; align-items:center;">
                                            <span>${cpuPercent.toFixed(1)}%</span>
                                            <div class="worker-mini-bar">
                                                <div class="worker-mini-fill" style="width: ${cpuPercent}%; background-color: var(--accent-blue);"></div>
                                            </div>
                                        </div>
                                    </div>
                                    <div class="worker-stat-bar">
                                        <span>Memory 사용률</span>
                                        <div style="display:flex; align-items:center;">
                                            <span>${memPercent.toFixed(1)}%</span>
                                            <div class="worker-mini-bar">
                                                <div class="worker-mini-fill" style="width: ${memPercent}%; background-color: var(--accent-purple);"></div>
                                            </div>
                                        </div>
                                    </div>
                                </div>

                                <div class="worker-heartbeat" style="${hbStyle}">
                                    <span>Last Heartbeat</span>
                                    <span>${hbAge}초 전</span>
                                </div>
                            </div>
                        `;
                    });
                    workersContainer.innerHTML = html;
                }

                // 4. Task Queue
                const queueContainer = document.getElementById("queue-container");
                const queueList = data.queue || [];
                document.getElementById("queue-count").innerText = `대기 ${queueList.length}개`;

                if (queueList.length === 0) {
                    queueContainer.innerHTML = `
                        <div class="empty-placeholder">
                            <svg fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2"/></svg>
                            <span>현재 대기 중인 태스크가 없습니다.<br>스케줄러가 부하 상황을 대기하고 있습니다.</span>
                        </div>
                    `;
                } else {
                    let html = "";
                    const now = Date.now() / 1000;
                    
                    queueList.forEach(task => {
                        const secondsLeft = Math.max(0, task.deadline - now);
                        const isOverdue = secondsLeft <= 0;
                        const timeText = isOverdue ? "Deadline 초과!" : `${secondsLeft.toFixed(1)}초 남음`;
                        
                        // Calculate percentage of deadline bar. Assume standard 30 sec scale
                        const rawTimeout = 30; 
                        const fillPct = isOverdue ? 0 : Math.min(100, (secondsLeft / rawTimeout) * 100);
                        
                        let barColor = "var(--accent-green)";
                        if (secondsLeft < 10) barColor = "orange";
                        if (secondsLeft < 5 || isOverdue) barColor = "var(--accent-red)";

                        html += `
                            <div class="task-card" style="${isOverdue ? 'border-color: rgba(239, 68, 68, 0.3);' : ''}">
                                <div class="task-header">
                                    <span class="task-id">${task.task_id}</span>
                                    <span class="task-model-badge">${task.model_type}</span>
                                </div>
                                <div class="task-details">
                                    <span>학습 epochs: ${task.epochs}회</span>
                                    <span style="font-weight: 600; color: ${barColor}">${timeText}</span>
                                </div>
                                <div class="task-deadline-bar">
                                    <div class="task-deadline-fill" style="width: ${fillPct}%; background-color: ${barColor};"></div>
                                </div>
                            </div>
                        `;
                    });
                    queueContainer.innerHTML = html;
                }

                // 5. Console logs
                const logs = data.logs || [];
                if (logs.length !== lastLogCount) {
                    const consoleLogs = document.getElementById("console-logs");
                    let logsHtml = "";
                    logs.forEach(log => {
                        logsHtml += `<div class="console-line">${colorizeLog(log)}</div>`;
                    });
                    consoleLogs.innerHTML = logsHtml;
                    consoleLogs.scrollTop = consoleLogs.scrollHeight;
                    lastLogCount = logs.length;
                }

            } catch (err) {
                document.getElementById("status-text").innerText = "GCS DISCONNECTED";
                document.getElementById("status-text").parentElement.style.background = "rgba(239, 68, 68, 0.1)";
                document.getElementById("status-text").parentElement.style.color = "var(--accent-red)";
                document.getElementById("status-text").parentElement.style.borderColor = "rgba(239, 68, 68, 0.2)";
                console.error("Dashboard update failed:", err);
            }
        }

        // 1.5초마다 데이터 업데이트
        setInterval(updateDashboard, 1500);
        updateDashboard();
    </script>
</body>
</html>
"""
