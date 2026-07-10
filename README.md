<div align="center">

# ⚡ WE-MEET: 클라우드 Docker 실증과 스케줄러 개발

### Docker 기반 이기종 ML 분산 학습 제어 엔진

*"단일 PC 위에서, 실제 클라우드처럼 — 비용과 처리량을 동시에 최적화한다"*

---

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=for-the-badge&logo=python&logoColor=white)
![gRPC](https://img.shields.io/badge/gRPC-Protobuf-244c5a?style=for-the-badge&logo=google&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-Compose-2496ED?style=for-the-badge&logo=docker&logoColor=white)
![Q-Learning](https://img.shields.io/badge/RL-Q--Learning-FF6B35?style=for-the-badge)
![cGroup](https://img.shields.io/badge/Linux-cGroup%20%2B%20MPS-FCC624?style=for-the-badge&logo=linux&logoColor=black)

</div>

---

## 🎯 한 줄 요약

> **"AWS/GCP 스팟 인스턴스 방식의 탄력적 이기종 클러스터를 단일 Windows PC에 구현하고, Q-Learning이 비용·SLA를 동시에 최적화하는 분산 ML 런타임입니다."**

---

## 📌 왜 만들었나요?

실제 클라우드 ML 워크플로우에는 두 가지 상충하는 목표가 있습니다.

| 목표 | 현실의 어려움 |
|---|---|
| 💰 **비용 절감** | Spot 인스턴스는 싸지만 언제든 강제 회수(Eviction)됨 |
| ⏱️ **SLA 준수** | 학습 마감을 지키려면 안정적인 On-Demand 자원이 필요 |

기존 스케줄러(FIFO, 정적 룰)는 이 트레이드오프를 **하드코딩된 규칙**으로만 처리합니다. WE-MEET은 **강화학습(Q-Learning)이 클러스터 상태를 6차원으로 인지하고 이 트레이드오프를 스스로 학습**하도록 설계되었습니다.

---

## 🏗️ 아키텍처 개요

```
┌─────────────────────────────────────────────────────────────────┐
│                        Head Node                                 │
│                                                                  │
│  ┌─────────────┐    ┌─────────────────────┐    ┌─────────────┐  │
│  │  Q-Learning │◄──►│  GCS (Global        │◄──►│  Dashboard  │  │
│  │  Scheduler  │    │  Control Store)     │    │  :8080      │  │
│  │  (6D State) │    │  + gcs_state.json   │    └─────────────┘  │
│  └──────┬──────┘    └─────────────────────┘                     │
│         │ Docker SDK                                             │
│         ▼  cGroup + NVIDIA MPS 동적 주입                         │
└─────────────────────────────────────────────────────────────────┘
          │ gRPC + Heartbeat
    ┌─────┴────────────────────────────────────┐
    │                                          │
┌───▼────────────┐  ┌──────────────┐  ┌───────▼──────────┐
│  Worker-1      │  │  Worker-2~N  │  │  Worker-2~N       │
│  On-Demand     │  │  Spot-A      │  │  Spot-B           │
│  CPU:2, Mem:2G │  │  CPU:1, Mem:1G│  │  CPU:0.5, Mem:512M│
│  $7.10/hr      │  │  $2.20/hr    │  │  $0.90/hr         │
│  Preempt: 0%   │  │  Preempt:50% │  │  Preempt: 10%     │
└────────────────┘  └──────────────┘  └──────────────────┘
CNN / RNN / LSTM 학습 워크로드 (PyTorch · cGroup · NVIDIA MPS 격리)
```

---

## ✨ 핵심 기술 포인트

### 1. 🧠 Q-Learning 6차원 상태 공간

단순한 큐 길이가 아닌, **6개 축의 조합**으로 상황을 인식합니다.

```python
State = (q_bucket, head_model, a_mix, sla_bucket, danger_phase, budget_level)
#         큐 적체    선두 모델    유휴 노드   SLA 임박   스팟 위험구간  잔여 예산
```

| Axis | 의미 | 값 |
|---|---|---|
| `q_bucket` | 큐 적체 깊이 | 0 (빈) / 1 (경) / 2 (중) / 3 (과) |
| `head_model` | 선두 태스크 성격 | 0 (연산집약·CNN) / 1 (메모리집약·LSTM) |
| `a_mix` | 유휴 노드 비트맵 | 0–7 (3비트 인코딩) |
| `sla_bucket` | SLA 마감 완급 | 0 (여유) / 1 (중간) / 2 (임박) |
| `danger_phase` | 스팟 회수 위험 구간 | 0 (안전) / 1 (위험) |
| `budget_level` | 잔여 예산 수준 | 0 (위험) / 1 (낮음) / 2 (여유) |

### 2. ⚖️ 3대 스케줄러 비교

| | Static | Dynamic | **Q-Learning** |
|---|---|---|---|
| 의사결정 | 큐 길이 임계치 룰 | CPU/MEM 부하 임계치 룰 | **6D 상태 + 강화학습** |
| Starvation 방지 | ❌ | ❌ | ✅ SLA 지연 페널티 (-5.0/s) |
| HOL Blocking 해결 | ❌ | ❌ | ✅ **OS Backfilling** |
| 자원 간섭 회피 | ❌ | ✅ | ✅ co-scheduling 보상 |
| 비용 인지 | ❌ | ❌ | ✅ 실시간 $0.2초 과금 |

### 3. 🔧 이기종 자원 격리 (cGroup + NVIDIA MPS)

소프트웨어 `sleep` 모사가 아닌, **하드웨어 수준**에서 성능 편차를 재현합니다.

- **CPU/MEM**: Docker cGroup `nano_cpus` · `mem_limit` 동적 주입
- **GPU**: `CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` 환경변수로 CUDA 스레드 물리 격리
  - On-Demand: 100% / Spot-A: 60% / Spot-B: 50%

### 4. 🛡️ 고가용성 & 자가 복구

```
스팟 노드 DEAD 판정 (3.0초 무응답)
        ↓
Task Lineage DAG 분석 → FAILED 서브태스크 식별
        ↓
최신 체크포인트(checkpoint_*_epoch_*.pt)에서 이어서 재학습
        ↓
Spot Worker 자동 Scale-Out 연동
```

- **GCS 상태 영속화**: `data/gcs_state.json` — Head 재시작 시 투명 리플레이
- **Skip-Execution**: 이미 완료된 서브태스크는 결과 캐시로 건너뜀
- **Task Rollback Guard**: 배정 실패 시 큐 최앞단으로 즉시 안전 회수

### 5. 💸 실시간 비용 청구 모델

```python
# 스케줄러 0.2초 루프마다 실시간 과금
budget -= (cost_per_hour / 3600.0) * 0.2
```

에이전트는 가상 예산($1.5 초기값)이 소진되는 압박을 느끼며 **적시 Scale-In을 자동 학습**합니다.

---

## 🚀 빠른 시작

### Docker Compose (권장)

```bash
# 1. 브릿지 네트워크 최초 1회 생성
docker network create babyray-net

# 2. 빌드 및 클러스터 전체 기동
docker-compose -f docker/docker-compose.yml up --build
```

📊 **실시간 모니터링**: http://localhost:8080

> **메모리 부족 경고 시 우회 방법** (PowerShell):
> ```powershell
> $env:BYPASS_RESOURCE_GUARD="1"; docker-compose -f docker/docker-compose.yml up --build
> ```

### 로컬 개발 (디버깅)

```bash
pip install -e .

# 터미널 1: Head Node (GCS + Scheduler)
python -m wemeet.transport.head

# 터미널 2: Worker Node
python -m wemeet.transport.worker --id worker-1 --type on_demand --port 50052 --head-host localhost --head-port 50051
```

### 3대 스케줄러 자동 비교 실험

```bash
docker network create babyray-net
python -m wemeet.experiment.run_real --budget 10 --chart
```

static → dynamic → q_learning 순차 실행 후 차트 자동 생성.

### 고속 시뮬레이션 벤치마크

```bash
python -m wemeet.simulation.benchmark --runs 10 --scenario normal,burst_heavy,low_budget --chart
```

---

## 📂 패키지 구조

> 코드는 **기능(feature) 기준**으로 단일 우산 패키지 `wemeet/` 아래 배치됩니다.

```
wemeet/
├── config/          # YAML 설정 단일 진실 (cost_model, sim_env)
├── transport/       # gRPC 통신 계층 (head / worker / proto)
├── cluster/         # Docker SDK 스케일 제어 + GCS 상태
├── scheduling/      # 스케줄러 루프 (static / dynamic / q_learning)
├── learning/        # Q-Learning 에이전트 + 보상 + 사전학습
├── simulation/      # 고속 벤치마크 엔진 + 실패 시뮬레이터
├── workload/        # CNN / RNN / LSTM 학습 태스크
└── observability/   # 실시간 대시보드 + 로그 + 리포팅
```

---

## 📊 OOM 확률 모델 (노드 × 모델)

작은 노드에 큰 모델을 얹으면 Q-Learning이 이를 **스스로 학습해 회피**합니다.

| 모델 | On-Demand (2GB) | Spot-A (1GB) | Spot-B (512MB) |
|---|---|---|---|
| **CNN** (연산집약) | 0% | 0% | 1% |
| **RNN** (균형) | 0% | 1% | 4% |
| **LSTM** (메모리집약) | 1% | 15% | **85%** |

---

## 📚 관련 문서

| 문서 | 설명 |
|---|---|
| [`project_proposal.md`](./project_proposal.md) | 기술 설계 전문 — 수식·프로토콜·상태공간 완전 명세 |
| [`patch_notes/`](./patch_notes/) | 버전별 변경 이력 및 설계 근거 로그 |
| [`docs/`](./docs/) | Docstring 스타일 가이드 (Google 규약) |
| [`references/`](./references/) | 학술 레퍼런스 분석서 |

---

## 👥 팀

| 역할 | 이름 | 담당 |
|---|---|---|
| 팀장 | 성시준 | 아키텍처 설계, gRPC, Q-Learning, GCS, 벤치마크 |

> **지산학교과 협력 프로젝트** | 클라우드컴퓨팅 전공 | 2026 하계

---

<div align="center">

*"Ray 논문의 사상을 오마주하여, 단일 PC 위에 탄력적 분산 ML 런타임을 직접 구현한 프로젝트"*

</div>
