# WE-MEET: Docker 기반 이기종 ML 분산 학습 제어 엔진 (GCS & Scheduler)

본 프로젝트는 단일 Windows 호스트 PC 환경(WSL2 기반 Docker) 하에서 **gRPC**, **리눅스 cGroup 자원 격리**, 그리고 **강화학습(Q-Learning) 및 OS 스케줄링 이론**을 융합하여 이기종 분산 인프라 상의 머신러닝 학습 연산을 실시간으로 제어하고 탄력적으로 스케일링하는 분산 학습 제어 엔진입니다.

---

## 📂 프로젝트 패키지 디렉토리 구조

코드는 **위치가 아니라 기능(feature)** 기준으로 단일 우산 패키지 `wemeet/` 아래에 재배치되어 있습니다(설치형 패키지 — `pip install -e .`). 각 서브패키지는 하나의 책임만 갖도록 결합도를 낮췄습니다.

```
WE-MEET/
  ├── wemeet/                          # 기능별 우산 패키지 (import 루트)
  │     ├── config/                    # 설정·상수 단일 진실(Source of Truth)
  │     │     ├── env_config.py        # cost_model.yaml + sim_env.yaml 로더(싱글턴·폴백 내장)
  │     │     ├── settings.py          # 포트·하트비트 주기 등 전역 상수 (구 common/config.py)
  │     │     ├── paths.py             # data/ 등 런타임 경로 단일 해석기 (CWD·파일깊이 무관)
  │     │     ├── cost_model.yaml      # 노드 요금·GPU 스펙·회수확률
  │     │     └── sim_env.yaml         # 확률/환경 변수(OOM·워크로드·보상·상태버킷·시나리오) 단일 파일
  │     ├── transport/                 # gRPC 통신 계층
  │     │     ├── head.py              # Head 부팅 진입점 (python -m wemeet.transport.head)
  │     │     ├── head_service.py      # Head gRPC 서비서 + 대시보드 상태 스냅샷
  │     │     ├── worker.py            # Worker 부팅 진입점 + 하트비트 클라이언트
  │     │     ├── worker_service.py    # Worker gRPC 서비서 (작업 수신/상태/자원조정)
  │     │     └── proto/               # babyray.proto + 생성 stub(babyray_pb2[_grpc])
  │     ├── cluster/                   # 자원·상태 관리
  │     │     ├── manager.py           # Docker SDK 스케일 제어 (구 cluster_manager.py)
  │     │     ├── resource_guard.py    # 호스트 물리 RAM/GPU VRAM 안전 가드
  │     │     └── gcs_state.py         # GCS 전역 인메모리 공유 상태 (구 state.py)
  │     ├── scheduling/                # 스케줄러 계층
  │     │     ├── daemon.py            # 중앙 스케줄러 루프 (구 scheduler_daemon.py)
  │     │     ├── executor.py          # 태스크 실행/복구·FedAvg 병합·체크포인트 (구 task_executor.py)
  │     │     ├── static.py            # Static (정적 룰 스텝)
  │     │     ├── dynamic.py           # Dynamic (동적 부하 스텝)
  │     │     └── qlearning_step.py    # Q-Learning 스케줄러 1주기 스텝 (구 scheduler/q_learning.py)
  │     ├── learning/                  # 강화학습 지능형 의사결정
  │     │     ├── agent.py             # Q-Learning Agent (6-Action·보상 수식)
  │     │     ├── state_features.py    # 상태 특징(State Feature) 단일 산출 모듈
  │     │     ├── reward_policy.py     # 비-ASSIGN(HOLD/SCALE) 보상 단일 산출 모듈
  │     │     └── pretrain.py          # 오프라인 사전 학습(Q-Table 수렴) 시뮬레이터
  │     ├── simulation/                # 확률 시뮬레이션·성능 측정
  │     │     ├── failure_simulator.py # OOM·Eviction·OutOfCapacity 확률 모델
  │     │     ├── fast_sim.py          # 고속 벤치마크 엔진(FastSimulator)
  │     │     └── benchmark.py         # 반복·통계·시나리오·리포트 (python -m wemeet.simulation.benchmark)
  │     ├── workload/                  # 연산 워커·모델
  │     │     ├── runner.py            # 실행기 계층 (구 gpu_simulator.py, FedAvg 병합 포함)
  │     │     ├── dummy_load.py        # PyTorch 부재 시 모델별 더미 부하 모사
  │     │     └── models/              # base·cnn·rnn·lstm 학습 태스크 구현
  │     └── observability/             # 관측(모니터링·로깅·리포트)
  │           ├── dashboard.py         # 실시간 대시보드 HTTP 서버 (Port: 8080)
  │           ├── logging.py           # 전역 이벤트 로그 채널(log_event) — dashboard와 분리
  │           ├── reporting_visualize.py / reporting_analyze.py  # 벤치마크 시각화·분석
  │           └── web/                 # 서빙되는 정적 프론트엔드 (index.html·app.js·style.css)
  ├── data/                            # 런타임 상태(q_table.json·gcs_state.json·벤치 CSV) — 패키지 밖(볼륨 마운트)
  ├── compile_proto.py                 # .proto → Python stub 컴파일 (저장소 루트)
  ├── pyproject.toml                   # 설치형 패키지 선언(setuptools packages.find = wemeet*)
  ├── docs/                            # DOCSTRING_STYLE.md(Google 규약) + pdoc 빌드 스크립트
  ├── patch_notes/                     # 변경 이력 및 설계 근거 로그
  ├── references/                      # 학술적 레퍼런스 분석서
  ├── project_proposal.md              # 시스템 설계 및 스케줄링 이론 종합 제안서
  └── docker/                          # 컨테이너화 빌드 및 compose 설정
        ├── Dockerfile.head            # Head Node용 도커 빌드 이미지 명세
        ├── Dockerfile.worker          # Worker Node용 도커 빌드 이미지 명세
        └── docker-compose.yml         # 이기종 클러스터 실증용 Compose 파일
```

> **설정 단일화**: 요금·GPU 스펙은 `wemeet/config/cost_model.yaml`, 그 외 모든 **확률/환경 변수**(OOM·회수·태스크 생성 확률·예산·보상 가중치·상태 버킷·시나리오 프리셋)는 `wemeet/config/sim_env.yaml` 한 곳에 모여 있고, 실제 경로·오프라인 학습·고속 벤치마크가 모두 `wemeet/config/env_config.py` 로더를 통해 같은 값을 참조합니다(드리프트 방지).

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
디버깅 목적 등으로 터미널에서 각각 프로세스를 띄워 테스트할 수 있습니다. 기능별 패키지(`wemeet/`)
재배치 이후에는 모듈 실행(`python -m ...`) 방식을 사용합니다(프로젝트 루트에서 실행하거나 `pip install -e .`).
```bash
# (권장) 편집 가능 설치 — sys.path 조작 없이 어디서든 임포트 가능
pip install -e .

# 터미널 1: Head Node (GCS 및 스케줄러 기동)
python -m wemeet.transport.head

# 터미널 2: Worker Node (포트 50052번에 수동 가동 및 마스터 연결)
python -m wemeet.transport.worker --id worker-1 --type on_demand --port 50052 --head-host localhost --head-port 50051

# (측정) 고속 벤치마크 시뮬레이터 — 통계·시나리오·리포트
python -m wemeet.simulation.benchmark --runs 10 --scenario normal,burst_heavy --chart
```

---

## 🛠️ 주요 기능 요약

1.  **3대 AI 모형 부하 시뮬레이션**: CNN(연산 지향), RNN(균형), LSTM(메모리 지향) 모형의 Epoch 연산 특징에 따른 물리 리소스 점유 시뮬레이터 구동. 호스트의 **NVIDIA MPS(Multi-Process Service) 물리 CUDA 스레드 격리**(`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` = `gpu_scale_factor * 100`) 환경변수를 활용하여 하드웨어 수준에서 이기종 성능 편차를 재현.
2.  **이기종 자원 격리 (cGroup & MPS)**: On-Demand / Spot-A / Spot-B 3종 노드의 CPU/MEM 자원 및 GPU MPS 스펙을 `wemeet/config/cost_model.yaml`에 정의하여 격리. 호스트 물리 RAM 감지에 따라 **스팟 확장 상한(MAX_SPOT_SCALE)을 동적으로 조절**하며, 스펙 설정을 `env_config` 로더로 동적 주입.
9.  **확률/환경 변수 단일화 & 발표용 측정 도구**: OOM·회수·워크로드 생성 확률 등 시뮬레이션 하이퍼파라미터를 `wemeet/config/sim_env.yaml` 한 파일로 통합(실제 경로·오프라인 학습·고속 sim이 공유). `python -m wemeet.simulation.benchmark`로 3대 스케줄러를 **N회 반복(시드 변동)** 측정하여 평균±표준편차·p50/p90·회수/OOM 분해 지표를 산출하고, 시나리오 프리셋별 마크다운 리포트를 자동 생성.
3.  **OS 스케줄링 기법 접목**: 선두 차단(HOL Blocking) 해결을 위한 **Backfilling** 스케줄러, 그리고 자원 기아(Starvation)를 방지하기 위해 마감 초과 시 초당 −5.0의 누적 감점을 부여하는 **SLA 마감 패널티** 및 6차원 상태 공간 내 긴급도 버킷(`sla_bucket`) 도입.
4.  **탄력성 & 고가용성**: 하트비트 **3.0초** 단절 감시(송신 주기 5.0초와의 오탐 트레이드오프 고려)를 통한 노드 장애 격리, **Task Lineage 기반 복구**(장애 서브태스크 재큐잉 + 자동 스케일아웃) 메커니즘 제공.
5.  **GCS 상태 영속화 (Checkpointing)**: 대기열·태스크 상태·Lineage·예산 등을 `data/gcs_state.json`에 저장하여 Head 재시작 시 중단 지점부터 투명 리플레이(2026-07-04).
6.  **체크포인트 이어서 재개 (Skip / Re-execution)**: 완료된 산출물(`final_*.pt`)은 건너뛰고, 중간 체크포인트(`checkpoint_*_epoch_*.pt`)가 있으면 최신 에포크부터 남은 만큼만 재학습하여 복구 오버헤드 최소화.
7.  **FedAvg 분산 병합 (Map-Merge)**: 큰 태스크를 최대 3개 Map으로 분할 후 MERGE(FedAvg) 단계에서 가중치를 수학적으로 병합하고 `[FedAvg Verification]` 검증 결론을 대시보드에 노출.
8.  **비용-SLA 트레이드오프 학습**: 요금·마감·co-scheduling을 반영한 보상으로 Q-Learning 에이전트가 적시 스케일인/아웃을 학습. AWS/GCP처럼 가동 중인 노드의 요금을 초 단위로 환산하여 매 0.2초 의사결정 루프마다 가상 예산(초기값 $1.5)에서 실시간 감산하는 **실시간 비용 청구 모델이 완비되어 가동 중**입니다.
