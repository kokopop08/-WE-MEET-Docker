# WE-MEET 패치 노트 (2026-06-29)

본 패치 노트는 오늘 수행된 스케줄러 복구, 메모리 모니터링 고도화 및 구문 정리 등의 주요 패치 내역과 변경 사유를 기록합니다.

---

## 1. 중앙 스케줄러 복구 및 위임 구조 단순화
* **오류 현상**: `head/scheduler.py` 파일의 인코딩 훼손 및 백업 코드 내 unexpected indent(들여쓰기) 오류로 인해 컴파일 에러가 발생하고 스레드가 즉시 중단되던 현상.
* **조치 내용**:
  * `scheduler.py` 파일을 처음부터 깨끗이 재작성하여 구문 에러를 차단했습니다.
  * 기존의 Q-Learning 전용 단일 의사결정 루프를 걷어내고, `state.SCHEDULER_MODE` 모드에 따라 3대 개별 스케줄러 모듈(`scheduler_static.py`, `scheduler_dynamic.py`, `scheduler_qlearning.py`)의 step 함수로 책임을 분리/위임하는 깔끔한 중앙 제어 구조를 정립했습니다.
  * 복잡한 Map-Reduce 태스크 쪼개기/합치기 및 FedAvg 가중치 병합 기능은 복잡도 감소를 위해 현 단계에서 배제하고 단일 태스크 처리 방식으로 안정화했습니다.

## 2. WSL2 / Docker RAM 감시 로직 고도화
* **오류 현상**: Windows 호스트에서 `psutil.virtual_memory()`만 체크할 경우, WSL2 및 Docker 컨테이너 내부의 메모리 고갈(OOM 일보직전) 상태를 감지하지 못하고 스케일아웃을 감행하는 자원 경합 현상.
* **조치 내용**:
  * Windows 호스트 환경(`os.name == 'nt'`)인 경우 `wsl free -b` 명령어를 호출하여 WSL2 VM 내부의 실질적 가용 메모리(`available`)를 파싱하여 2.0GB 미만 시 스케일아웃을 안전하게 제어합니다.
  * Linux/WSL2 내부 실행 시에는 directly `free -b` 명령어를 파싱합니다.
  * 명령어 호출 실패 시에는 기존의 `psutil` 기반 호스트 메모리 점검으로 안전하게 폴백(Fallback)하도록 설계했습니다.

## 3. 스케줄러 기본 구동 모드 변경
* **조치 내용**: 기본 구동 스케줄러 모드를 `"q_learning"`에서 **`"dynamic"` (동적 부하 인지형 스케줄러)**으로 변경하여 시스템 기동 시 자동으로 가변 CPU/MEM 부하를 인지해 노드를 조절하도록 설정했습니다.

## 4. 모듈 임포트 충돌 해결 및 스케일 메시지 정돈
* **조치 내용**:
  * `run_task_on_worker` 함수 내에서 로컬 매개변수 `state`와 전역 `state` 모듈 간의 이름 충돌(Shadowing)로 인한 에러를 방지하고자, 임포트 별칭을 `import state as gcs_state`로 통일했습니다.
  * `head/cluster_manager.py` 내의 컨테이너 기동/회수 로그 메시지들의 접두어를 `[Scale-Out]` 및 `[Scale-In]`으로 통일하여 터미널 로그의 모니터링 시인성을 개선했습니다.
