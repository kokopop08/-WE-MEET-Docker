# WE-MEET: Docker 기반 이기종 ML 분산 학습 제어 엔진 (GCS & Scheduler)

본 프로젝트는 단일 Windows 호스트 PC 환경(WSL2 기반 Docker) 하에서 **gRPC**, **리눅스 cGroup 자원 격리**, 그리고 **강화학습(Q-Learning) 및 OS 스케줄링 이론**을 융합하여 이기종 분산 인프라 상의 머신러닝 학습 연산을 실시간으로 제어하고 탄력적으로 스케일링하는 분산 학습 제어 엔진입니다.

---

## 📂 프로젝트 패키지 디렉토리 구조

프로젝트의 전반적인 모듈은 결합도를 낮추고 유지 보수성을 높이기 위해 다음과 같이 역할군에 따라 패키지화되었습니다.

```
WE-MEET/
  ├── head/                      # GCS 및 마스터(Head) 노드 패키지
  │     ├── head.py              # [인프라] gRPC 마스터 서버 기동 엔트리포인트
  │     ├── state.py             # [인프라] GCS 전역 인메모리 공유 상태 정의
  │     ├── cluster_manager.py   # [인프라] WSL2 리소스 가드 및 Docker SDK 스케일 제어
  │     ├── scheduler/           # 스케줄러 계층 패키지
  │     │     ├── __init__.py
  │     │     ├── scheduler_daemon.py # 중앙 스케줄러 스레드 루프 (Backfilling·Map-Merge·DEAD 복구 탑재)
  │     │     ├── task_executor.py    # 태스크 실행/복구·FedAvg 병합·체크포인트 정리 유틸
  │     │     ├── static.py      # Static (정적 룰 스텝) 스케줄러
  │     │     └── dynamic.py     # Dynamic (동적 부하 스텝) 스케줄러 [기본 구동 모드]
  │     ├── q_learning/          # 지능형 의사결정 Q-Learning 패키지
  │     │     ├── __init__.py
  │     │     ├── agent.py       # Q-Learning Agent 클래스 (6-Action·보상 수식 탑재)
│     │     ├── state_features.py # Q-Learning 상태 특징(State Feature) 단일 산출 모듈
  │     │     ├── pretrain.py    # 오프라인 사전 학습(Q-Table 수렴) 시뮬레이터
  │     │     └── q_table.json   # 강화학습 경험 축적 파일
  │     └── dashboard/           # 모니터링 대시보드 웹 서비스 패키지
  │           ├── __init__.py
  │           ├── server.py      # 실시간 대시보드 HTTP 서버 (Port: 8080)
  │           └── web/           # 실제 서빙되는 정적 프론트엔드 (index.html·app.js·style.css)
  ├── worker/                    # 분산 학습 연산 워커(Worker) 노드 패키지
  │     ├── worker.py            # 워커 gRPC 서비서 및 하트비트 클라이언트
  │     ├── gpu_simulator.py     # CNN/RNN/LSTM 연산 속도 및 하드웨어 점유 시뮬레이터 (FedAvg 병합 포함)
  │     └── models/              # 모델별 학습 태스크 구현
  │           ├── base.py        # BaseTask 추상 인터페이스
  │           ├── cnn.py         # SimpleCNN (이미지 분류)
  │           ├── rnn.py         # SimpleRNN (시계열 예측)
  │           └── lstm.py        # SimpleLSTM (자연어 처리)
  ├── common/                    # 공유 라이브러리 및 하이퍼파라미터 설정
  │     ├── config.py            # 포트·하트비트 주기 등 전역 상수
  │     └── cost_model.yaml      # 이기종 인스턴스 요금 및 GPU 성능 스펙 파일
  ├── proto/                     # gRPC 인터페이스 버퍼 정의 및 컴파일 산출물
  │     ├── babyray.proto        # Protobuf 서비스/메시지 정의
  │     ├── babyray_pb2.py       # (생성물) 메시지 stub
  │     └── babyray_pb2_grpc.py  # (생성물) 서비스 stub
  ├── compile_proto.py           # .proto → Python stub 컴파일 스크립트 (저장소 루트)
  ├── references/                # 학술적 레퍼런스 분석서
  │     └── mentoring_ref.md     # 선행 연구 분석 및 극복 방향 기술
  ├── project_proposal.md        # 시스템 설계 및 스케줄링 이론 종합 제안서
  └── docker/                    # 컨테이너화 빌드 및 compose 설정 디렉토리
        ├── Dockerfile.head      # Head Node용 도커 빌드 이미지 명세
        ├── Dockerfile.worker    # Worker Node용 도커 빌드 이미지 명세
        └── docker-compose.yml   # 이기종 클러스터 실증용 Compose 파일
```

---

## 🚀 기동 및 실행 가이드

### 1. Docker Compose 기반 클러스터 기동 (권장)
동적 스케일링 중인 탄력 워커 노드들의 안전한 라이프사이클 관리와 셧다운 시 리소스 누수(Network Resource is still in use)를 완천 차단하기 위해 고정 외부 브릿지 네트워크를 이용합니다.

```bash
# 1. 외부 결합 브릿지 네트워크 사전 생성 (최초 1회 필수 실행)
docker network create babyray-net

# 2. 클러스터 전체 빌드 및 가동
docker-compose -f docker/docker-compose.yml up --build
```

*   **실시간 모니터링 웹 대시보드**: 브라우저를 열어 [http://localhost:8080](http://localhost:8080) 에 접속하면 프리미엄 다크모드 글래스모피즘 화면을 통해 현재 큐 상태, 강화학습 지표(Epsilon), 워커 풀별 라이브 트랜지션 스케일인/아웃 애니메이션을 볼 수 있습니다.
*   **자원 가드 우회(Bypass) 꿀팁**:
    로컬 시스템 가용 메모리가 부족하여 시작 직후 스케일아웃 경고 로그가 지속된다면, 아래 환경 변수를 부여하여 호스트의 안전 검사를 강제 바이패스해 볼 수 있습니다.
    * **PowerShell:** `$env:BYPASS_RESOURCE_GUARD="1"; docker-compose -f docker/docker-compose.yml up --build`
    * **Bash/CMD:** `BYPASS_RESOURCE_GUARD=1 docker-compose -f docker/docker-compose.yml up --build`
*   **클러스터 중단 및 완전 회수**:
    ```bash
    docker-compose -f docker/docker-compose.yml down
    ```

### 2. 로컬 가상환경 수동 개별 기동
디버깅 목적 등으로 터미널에서 각각 프로세스를 띄워 테스트할 수 있습니다.
```bash
# 터미널 1: Head Node (GCS 및 스케줄러 기동)
python head/head.py

# 터미널 2: Worker Node (포트 50052번에 수동 가동 및 마스터 연결)
python worker/worker.py --id worker-1 --type on_demand --port 50052 --head-host localhost --head-port 50051
```

---

## 🛠️ 주요 기능 요약

1.  **3대 AI 모형 부하 시뮬레이션**: CNN(연산 지향), RNN(균형), LSTM(메모리 지향) 모형의 Epoch 연산 특징에 따른 물리 리소스 점유 시뮬레이터 구동. 호스트의 **NVIDIA MPS(Multi-Process Service) 물리 CUDA 스레드 격리**(`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` = `gpu_scale_factor * 100`) 환경변수를 활용하여 하드웨어 수준에서 이기종 성능 편차를 재현.
2.  **이기종 자원 격리 (cGroup & MPS)**: On-Demand / Spot-A / Spot-B 3종 노드의 CPU/MEM 자원 및 GPU MPS 스펙을 `cost_model.yaml`에 정의하여 격리. 호스트 물리 RAM 감지에 따라 **스팟 확장 상한(MAX_SPOT_SCALE)을 동적으로 조절**하며, 스펙 설정을 동적으로 로드 및 주입.
3.  **OS 스케줄링 기법 접목**: 선두 차단(HOL Blocking) 해결을 위한 **Backfilling** 스케줄러, 그리고 자원 기아(Starvation)를 방지하기 위해 마감 초과 시 초당 −5.0의 누적 감점을 부여하는 **SLA 마감 패널티** 및 6차원 상태 공간 내 긴급도 버킷(`sla_bucket`) 도입.
4.  **탄력성 & 고가용성**: 하트비트 **3.0초** 단절 감시(송신 주기 5.0초와의 오탐 트레이드오프 고려)를 통한 노드 장애 격리, **Task Lineage 기반 복구**(장애 서브태스크 재큐잉 + 자동 스케일아웃) 메커니즘 제공.
5.  **GCS 상태 영속화 (Checkpointing)**: 대기열·태스크 상태·Lineage·예산 등을 `data/gcs_state.json`에 저장하여 Head 재시작 시 중단 지점부터 투명 리플레이(2026-07-04).
6.  **체크포인트 이어서 재개 (Skip / Re-execution)**: 완료된 산출물(`final_*.pt`)은 건너뛰고, 중간 체크포인트(`checkpoint_*_epoch_*.pt`)가 있으면 최신 에포크부터 남은 만큼만 재학습하여 복구 오버헤드 최소화.
7.  **FedAvg 분산 병합 (Map-Merge)**: 큰 태스크를 최대 3개 Map으로 분할 후 MERGE(FedAvg) 단계에서 가중치를 수학적으로 병합하고 `[FedAvg Verification]` 검증 결론을 대시보드에 노출.
8.  **비용-SLA 트레이드오프 학습**: 요금·마감·co-scheduling을 반영한 보상으로 Q-Learning 에이전트가 적시 스케일인/아웃을 학습. AWS/GCP처럼 가동 중인 노드의 요금을 초 단위로 환산하여 매 0.2초 의사결정 루프마다 가상 예산(초기값 $1.5)에서 실시간 감산하는 **실시간 비용 청구 모델이 완비되어 가동 중**입니다.
