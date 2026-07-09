# 🛠️ Baby Ray: 양방향 gRPC API 및 기술 명세서

본 문서는 Docker 기반 경량 분산 런타임(Baby Ray) 프로젝트의 **1단계: 양방향 작업 할당 및 모니터링 통신 설계 명세서**입니다. Head 노드와 Worker 노드가 각각 서버와 클라이언트 역할을 유기적으로 분담하는 고속 비동기 제어 구조 및 API 메시지 규격을 상세히 기술합니다.

---

## 1. 양방향 gRPC 통신 아키텍처 개요

본 프로젝트에서는 단방향 폴링이나 단순 스트리밍 한계를 극복하고, 노드 간 명령을 능동적으로 주고받기 위해 **양방향 서버-클라이언트 아키텍처**를 채택했습니다.

```
┌──────────────────────────────────────┐                   ┌──────────────────────────────────────┐
│       Head Node (스케줄러/GCS)        │                   │             Worker Node              │
│  • gRPC 서버 (Port 50051)            │ ◄──────────────── │  • gRPC 클라이언트 (Heartbeat 송신)   │
│  • gRPC 클라이언트 (Task 배정 및 폴링)  │ ────────────────► │  • gRPC 서버 (Port 50052 등 개방)     │
└──────────────────────────────────────┘                   └──────────────────────────────────────┘
```

1. **상향 통신 (Worker ──► Head)**:
   - Worker가 기동되면 Head 서버(50051)로 자신을 **등록(RegisterWorker)**하고, **5.0초 주기**(`common/config.py:DEFAULT_HEARTBEAT_INTERVAL = 5.0`)로 CPU/Memory 사용률 등의 생존 신고 메트릭을 **전송(SendHeartbeat)**합니다. 정상 종료 시에는 **등록 해제(DeregisterWorker)**로 자신을 퇴장 처리합니다.
2. **하향 통신 (Head ──► Worker)**:
   - Head의 백그라운드 스케줄러가 가용(IDLE) 워커를 탐색하여 작업을 비동기로 **배정(AssignTask)**하고, 진행률과 가상 학습 로그를 주기적으로 **조회(GetTaskStatus)**합니다(폴링 주기: 단일 태스크 1.5초, 맵/머지 서브태스크 1.0초). 필요 시 cGroup 자원 한도를 **동적 변경(ResizeResources)**합니다.

> **타임아웃 처리 방식**: `AssignTask`/`GetTaskStatus` 호출에는 gRPC deadline을 명시적으로 설정하지 않으며, 매 폴링마다 GCS 워커 레지스트리 생존 여부를 재확인하여 워커 소실 시 `grpc.RpcError`를 유발, 작업을 대기열 선두로 롤백합니다. Head가 특정 워커의 Heartbeat를 **3.0초** 동안 수신하지 못하면 해당 노드를 `DEAD`로 판정합니다(단, `worker-1`/`on_demand`는 판정 제외).

---

## 2. Protobuf 정의 및 메시지 명세 ([babyray.proto](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/proto/babyray.proto))

gRPC 인터페이스 및 직렬화 스펙 정의 파일의 메시지 필드 규격입니다.

### 서비스 정의 (`BabyRayService`)
```protobuf
syntax = "proto3";

package babyray;

service BabyRayService {
  // Worker -> Head: 가상 노드 생명주기 관리
  rpc RegisterWorker (RegisterRequest) returns (RegisterResponse);
  rpc DeregisterWorker (DeregisterRequest) returns (DeregisterResponse);

  // Worker -> Head: 생존 신고 및 자원 상태 리포트
  rpc SendHeartbeat (HeartbeatRequest) returns (HeartbeatResponse);

  // Head -> Worker: 비동기 작업 제어 및 모니터링
  rpc AssignTask (TaskAssignment) returns (TaskResult);
  rpc GetTaskStatus (TaskStatusRequest) returns (TaskStatusResponse);

  // Head -> Worker: 자원 격리 한도 조정
  rpc ResizeResources (ResizeRequest) returns (ResizeResponse);
}
```

---

### API 상세 메시지 규격

#### 1) RegisterWorker
Worker가 시작 시 Head 노드에 자신을 등록하여 통신 주소와 유형을 알립니다.
* **`RegisterRequest` (요청)**:
  - `worker_id` (string): 워커 고유 식별 식별자 (예: `"worker-01"`)
  - `node_type` (string): 노드 타입 (예: `"on_demand"`, `"spot_a"`, `"spot_b"`)
  - `port` (int32): 워커 본인이 개방하여 명령을 대기 중인 gRPC 서버 포트 (예: `50052`)
* **`RegisterResponse` (응답)**:
  - `success` (bool): 등록 승인 성공 여부 (`true` / `false`)
  - `message` (string): 등록 처리 결과 관련 안내 문자열

#### 2) SendHeartbeat
Worker가 자신의 자원 실시간 메트릭 및 생존 여부를 주기적으로 Head에 전송합니다.
* **`HeartbeatRequest` (요청)**:
  - `worker_id` (string): 워커 식별 고유 ID
  - `cpu_utilization` (float): 실시간 CPU 사용률 (%)
  - `memory_utilization` (float): 실시간 메모리 사용률 (%)
* **`HeartbeatResponse` (응답)**:
  - `ack` (bool): 수신 승인 플래그 (`true`)

#### 3) AssignTask
Head가 가용한 Worker에게 비동기 가상 ML 모델 학습 작업을 위임합니다.
* **`TaskAssignment` (요청)**:
  - `task_id` (string): 스케줄러가 발급한 고유 작업 ID (예: `"task-0001"`)
  - `model_type` (string): 실행할 AI 연산 모델 종류 (`CNN`, `RNN`, `LSTM`)
  - `dataset_path` (string): 학습에 사용할 데이터셋 경로
  - `epochs` (int32): 수행할 가상 학습 에포크 수
* **`TaskResult` (응답)**:
  - `task_id` (string): 대상 작업 ID
  - `status` (string): 즉시 수락 상태 (`RUNNING`, `FAILED`)
  - `execution_time` (float): 초기 할당 시에는 `0.0` 반환
  - `message` (string): 작업 접수 관련 세부 텍스트

#### 4) GetTaskStatus
Head가 배정한 가상 작업의 진행 과정 및 에포크 로그 정보를 폴링(Polling) 조회합니다.
* **`TaskStatusRequest` (요청)**:
  - `task_id` (string): 모니터링 대상 가상 작업 ID
* **`TaskStatusResponse` (응답)**:
  - `status` (string): 현재 실행 상태 (`RUNNING`, `SUCCESS`, `FAILED`)
  - `progress` (float): 연산 진행률 백분율 (`0.0` ~ `100.0` %)
  - `logs` (string): 현재까지 누적된 가상 에포크별 로그 결과 데이터 (FedAvg 병합 시 `[FedAvg Verification]` 검증 라인 포함)

> **참고 (MERGE/FedAvg)**: `AssignTask`의 `model_type`에 `"MERGE"`를 지정하고 `dataset_path`를 `"merge:<경로1>,<경로2>,..."` 형식으로 전달하면, 워커가 각 Map 산출 가중치(`state_dict`)를 로드해 FedAvg(평균) 병합을 수행하고 `data/final_{task_id}.pt`를 산출합니다. (병합 태스크 ID 접미사: `-merge`)

#### 5) DeregisterWorker
Worker가 정상 종료 시 Head에 자신의 등록을 해제하여 GCS 풀에서 즉시 제거되도록 요청합니다.
* **`DeregisterRequest` (요청)**:
  - `worker_id` (string): 등록 해제할 워커 고유 ID
* **`DeregisterResponse` (응답)**:
  - `success` (bool): 해제 처리 성공 여부
  - `message` (string): 처리 결과 안내 문자열

#### 6) ResizeResources
Head가 워커의 cGroup 자원 격리 한도(CPU 코어 수, 메모리)를 런타임에 동적으로 조정합니다.
* **`ResizeRequest` (요청)**:
  - `cpu_cores` (float): 조정할 CPU 코어 수
  - `memory_bytes` (int64): 조정할 메모리 한도(bytes)
* **`ResizeResponse` (응답)**:
  - `success` (bool): 조정 적용 성공 여부
  - `message` (string): 처리 결과 안내 문자열

---

## 3. 통신 시퀀스 다이어그램 (Sequence Diagram)

```mermaid
sequenceDiagram
    autonumber
    participant H as Head Node (Server/Client)
    participant W as Worker Node (Server/Client)

    Note over H: gRPC 서버 시작 (Port 50051)
    Note over W: gRPC 서버 시작 (Port 50052)
    
    W->>H: RegisterWorker(worker_id="worker-01", node_type="on_demand", port=50052)
    Note over H: GCS 인메모리 풀에 워커 주소(127.0.0.1:50052) 등록
    H-->>W: RegisterResponse(success=true)
    
    loop 5초 주기 생존 신고
        W->>H: SendHeartbeat(worker_id="worker-01", cpu=12.5%, mem=40.0%)
        H-->>W: HeartbeatResponse(ack=true)
    end

    Note over H: 스케줄러가 IDLE 워커 발견
    H->>W: AssignTask(task_id="task-0001", model_type="CNN", epochs=5)
    Note over W: 백그라운드 스레드에서 가상 학습 실행기(MockTaskRunner) 기동
    W-->>H: TaskResult(status="RUNNING")

    loop 주기적(1.0~1.5초) 진행률 모니터링 폴링
        H->>W: GetTaskStatus(task_id="task-0001")
        W-->>H: TaskStatusResponse(status="RUNNING", progress=40.0%, logs="Epoch 1/5...")
    end
    
    Note over W: 가상 에포크 5/5 완료 (SUCCESS)
    
    H->>W: GetTaskStatus(task_id="task-0001")
    W-->>H: TaskStatusResponse(status="SUCCESS", progress=100.0%, logs="Epoch 1/5...\nEpoch 5/5...")
    Note over H: 워커의 최종 로그를 수집하여 터미널 화면에 출력
    Note over H: 워커 상태를 IDLE로 환원
```

---

## 4. 포터블 로컬 검증 CLI 명령어

본 코드는 팀원 간 절대 경로 충돌이 일어나지 않도록 상대 경로 패치가 완료되어 있으며, 터미널 실행 또는 동봉된 배치 파일을 통해 즉시 작동 검증이 가능합니다.

### 0. (최초 1회) Protobuf 컴파일
`.proto` 정의를 수정한 경우, 저장소 루트의 컴파일 스크립트로 stub을 재생성합니다.
```powershell
.\.venv\Scripts\python.exe compile_proto.py
```

### Windows (PowerShell) 구동 명령어
1. **Head 노드 실행 (터미널 1)**:
   ```powershell
   .\.venv\Scripts\python.exe head\head.py
   ```
2. **Worker 노드 실행 (터미널 2)** — `--type`은 `on_demand` / `spot_a` / `spot_b` 중 하나:
   ```powershell
   .\.venv\Scripts\python.exe worker\worker.py --id worker-1 --type on_demand --port 50052 --head-host localhost --head-port 50051
   ```

### Docker Compose 기반 클러스터 일괄 기동 (권장)
```powershell
docker network create babyray-net   # 최초 1회
docker-compose -f docker/docker-compose.yml up --build
```
실시간 모니터링 대시보드: [http://localhost:8080](http://localhost:8080)

> 참고: 별도의 `run_head.bat` / `run_worker.bat` 원클릭 배치 파일은 현재 저장소에 포함되어 있지 않습니다. 위 CLI 또는 Docker Compose 방식을 사용하세요.
