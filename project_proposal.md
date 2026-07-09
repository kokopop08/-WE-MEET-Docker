# Baby Ray 프로젝트 수행계획서 및 기술제안서 (통합 개편 최종본)

본 문서는 이기종 가상 클러스터 기반 ML 분산 학습 제어 엔진인 **WE-MEET**의 프로젝트 수행 계획과 기술 설계 명세서입니다. 탄력적인 가상 클라우드 인프라의 요금제 및 이질성을 활용하여, OOM 병목을 회피하고 가용 비용 대비 분산 학습 Throughput을 자동 극대화하는 탄력적 지능형 제어 엔진을 목표로 정립하였습니다. 

> 📌 **정합화 기준(2026-07-09)**: 본 기술제안서의 모든 수치·규격은 07-09 최종 패치가 반영된 **실제 소스 코드를 기준(Source of Truth)**으로 재검증 및 완전 일치하도록 동기화되었습니다.

---

# [Part 1] 프로젝트 수행계획서

## 1. 과제 개요 및 주요 목표
*   단일 Windows 호스트 PC 환경(WSL2 기반 Docker) 하에서 gRPC, 리눅스 cGroup 자원 격리, 그리고 강화학습(Q-Learning) 및 OS 스케줄링 이론을 융합하여 탄력적인 가상 클라우드 인프라의 요금제 및 이질성을 활용하고, OOM 병목을 회피하여 가용 비용 대비 분산 학습 Throughput을 자동 극대화하는 탄력적 지능형 제어 엔진을 구현합니다.
*   학습 모델(CNN, RNN, LSTM)의 고유한 자원 요구도 특성에 대응하여 물리적인 자원 격리를 시연하고 최적의 가변 요금제(Spot 요금제) 스케줄링 정책을 학습해 냄으로써 실제 분산 컴퓨팅 런타임의 최적 자원 배분 메커니즘을 증명하는 것을 최종 목표로 합니다.

## 2. 주요 추진 목표 및 핵심 구현 리스트
*   **이기종 가상 성능 시뮬레이션**: 단일 호스트 내에서 인위적인 소프트웨어 `sleep` 지연 대신, 호스트의 **NVIDIA MPS(Multi-Process Service) 물리 CUDA 스레드 할당 격리** 환경변수를 활용하여 노드별 물리 성능 편차 구현.
    *   **Worker-1**: On-Demand (Scale 1.0, CPU 2.0 Cores, Mem 2GB, 상시 고정 노드)
    *   **Worker-2~N**: Spot-A (Scale 0.6, CPU 1.0 Core, Mem 1GB) 및 Spot-B (Scale 0.5, CPU 0.5 Core, Mem 512MB) — 동적 스케일아웃으로 확장되며 컨테이너 번호(index)를 재사용.
    *   **스팟 확장 상한**: 호스트 물리 RAM 용량을 감지하여 **동적으로 스케일 상한(MAX_SPOT_SCALE)을 조절**하며, `scheduler_daemon.py`에서 관장. 스팟 상한 판정 시 실제 가동 중인 `spot_a`와 `spot_b` 노드의 총 대수를 정확히 합산하여 제한을 적용합니다.
*   **3대 머신러닝 워크로드 구성**:
    *   **이미지 분류 (CNN)**: SimpleCNN (GPU 연산 집약형, Spot 절감 검증용)
    *   **시계열 예측 (RNN)**: SimpleRNN (CPU/GPU 균형 연산형, 노드 이종성 검증용)
    *   **자연어 처리 (LSTM)**: SimpleLSTM (메모리 집약형, cGroup 제한 측정용)
*   **Task Lineage DAG 기반 장애 자가 복구**:
    *   Heartbeat 3.0초 미수신 시 DEAD 판정 ➔ GCS의 Task Lineage DAG 분석 ➔ 의존 하위 태스크 식별 ➔ 최신 체크포인트부터 학습 재개 및 Auto Scale-out 연동.
*   **고가용성 및 클러스터 안전 가드 장치 (Fault-Tolerance & Guard Systems)**:
    *   **비동기 좀비 컨테이너 클리너 (Async GCS Cleaner)**: Head Node 초기 구동 시, 호스트에 잔존하던 과거의 비정상 종료 Spot 컨테이너 잔해를 검출하여 비동기 데몬 스레드로 백그라운드 소거. gRPC 소켓 바인딩 및 서비스 시작을 차단하던 구버전 삭제 딜레이를 해결.
    *   **호스트 물리 메모리 Guard (Host Memory Guard)**: 스케일아웃 기동 시 `psutil.virtual_memory().percent`로 호스트 물리 메모리 사용률을 측정하여 사용률이 85.0%를 초과하면 추가 컨테이너 배포를 거부·보류하여 호스트 OS의 OOM 붕괴를 방지합니다. WSL2 환경에서는 컨테이너 내부 `free -b` 측정치와 비교하여 더 큰 사용률을 채택(보수적 판정)합니다. 환경변수 `BYPASS_RESOURCE_GUARD=1` 설정 시 이 가드를 단락(short-circuit) 우회합니다.
    *   **가용 GPU VRAM Guard (VRAM Guard)**: `nvidia-smi --query-gpu=memory.free`로 가용 GPU 메모리를 조회하여 500 MiB 미만일 때 스케일아웃을 긴급 차단하여 VRAM 고갈에 따른 CUDA 연산 크래시를 차단합니다.
    *   **작업 분배 롤백 방어 (Task Rollback Guard)**: 다중 배정(Multi-Dispatch) 과정에서 경합 조건 등으로 인해 특정 워커로의 태스크 바인딩 및 연산 위임에 실패할 경우, 작업을 삭제하지 않고 GCS 대기열의 맨 앞(`insert(0, task)`)으로 즉각 안전하게 회수 및 롤백.

## 3. 주차별 추진 일정 (상세 일정표)

| 주차/일차 | 팀 목표 및 활동 | 성시준 (팀장) 역할 | 김현진 (팀원) 역할 | 투입시간 |
| :--- | :--- | :--- | :--- | :--- |
| **0주차** | 가상 자원 분산 환경 가설 검토 및 계획서 구체화 | gRPC 프로토콜 분석, 뼈대 설계 | 운영 자동화 및 도입 타당성 검토 | 4 |
| **1일차 (6.22)** | 프로젝트 주제 구체화 및 방향성 탐색 | 팀 빌딩 및 분산 컴퓨팅 연구 방향 정의 | 로컬 가상화 도입 타당성 검토 | 6 |
| **2일차 (6.23)** | 가상 분산 런타임 통신 구조 선행 분석 | gRPC 및 생존 확인 통신 구조 기획 | 인프라 배포 자동화 스크립트 설계 | 8 |
| **3일차 (6.24)** | 통신 인터페이스 메시지 규격 설계 | Protobuf 기반 데이터 송수신 규격 설계 | 가상 인프라 모니터링 변수 정의 | 8 |
| **4일차 (6.25)** | 가상 인프라 자원 연동 구조 설계 | Docker cGroup 자원 제한 메커니즘 설계 | 단독 노드 기반 AI 연산 파이프라인 개발 | 8 |
| **5일차 (6.26)** | 0주차 마일스톤 점검 및 저장소 초기화 | 수행계획서 취합 및 GitHub 저장소 설정 | 학습 데이터셋 합성 및 전처리 완료 | 8 |
| **6일차 (6.29)** | 강화학습 의사결정 액션 최적화 및 감쇄 도입 | Action Space 4개 축소 및 Epsilon Decay 설계 구현 | 에이전트 모듈 리팩토링 및 캘리브레이션 | 8 |
| **7일차 (6.30)** | 코드 표준화 및 다큐멘테이션 보강 | 기존 주석 완벽 보존하며 표준 Docstring 추가 | PDF 명세서 추출용 포맷 정돈 | 8 |
| **8일차 (7.01)** | 의존성 기반 Task Lineage DAG 개발 | Lineage 의존성 탐색 및 Cascaded Recovery 구현 | 장애 상황별 복구 시나리오 테스트 보조 | 8 |
| **9일차 (7.02)** | 오프라인 가상 시뮬레이션 기반 고속 학습 | 사전 학습 시뮬레이터 작성 및 Q-Table 최적 수렴 | 에피소드 반복 횟수별 학습 데이터 추출 | 10 |
| **10일차 (7.03)** | 실환경 컨테이너 연동 및 검증 | GCS 연동 미세 조정 및 가상 OOM 안정성 검증 | 실환경 벤치마크 학습 로그 분석 | 6 |
| **11일차 (7.06)** | 연산 부하 평준화 및 자원 균일화 모델 보강 | 3대 ML 워크로드 파라미터 튜닝 및 OOM 방어 적용 | 모의 부하에 따른 cGroup 자원 분석 | 8 |
| **12일차 (7.07)** | 시뮬레이터 속도 지연 캘리브레이션 | 이기종 노드별 수행 시간 및 비용 추이 매핑 | 벤치마크 테스트 데이터 로깅 자동화 | 8 |
| **13일차 (7.08)** | 스케줄러 알고리즘 비교 실험 (RL vs HEUR) | Q-Learning vs FIFO/Random 비교 실험 수행 | 알고리즘별 SLA 준수율 및 비용 통계 정리 | 8 |
| **14일차 (7.09)** | 분산 클러스터 탄력성 벤치마크 수행 | Spot 워커 다수(최대 7대) 증설 환경 가상 확장 및 부하 테스트 | 오토스케일링 딜레이에 따른 병목 수치화 | 7 |
| **15일차 (7.10)** | 통합 자원 가드 보호 기전 극한 검증 | Host/GPU 메모리 경계 조건 강제 진입 및 차단 검증 | OOM Preemption Guard 무결성 평가 | 8 |
| **16일차 (7.13)** | 벤치마크 데이터 시각화 | Throughput 및 비용 소모 추이 시각화 그래프 구현 | 최종 분석용 벤치마크 이미지 패키징 | 8 |
| **17일차 (7.14)** | 시스템 개선 및 멘토링 피드백 반영 | 의사결정 수렴 상태 최종 점검 및 미세 튜닝 | 시각화 보고서 오류 보정 및 수정 | 8 |
| **18일차 (7.15)** | 최종 시스템 배포 빌드 정돈 | Docker Compose 및 SDK 통합 구동 스크립트 작성 | 최종 런타임 안정성 총괄 점검 | 8 |
| **19일차 (7.16)** | 최종 결과 보고서 종합 작성 | 기술 산출물 문서 패키징 및 보고서 검토 | 결과보고서 본문 기술 문서 종합 작성 | 8 |
| **20일차 (7.17)** | 프로젝트 최종 제출 및 마감 | 최종 보고서 검토 및 최종 검수 | 최종 과제물 및 산출물 업로드 완료 | 4 |

---

# [Part 2] 기술제안서 (Technical Proposal)

## 1. 분산 아키텍처 및 제어 토폴로지

본 프로젝트는 **1 Head Node - $N$ Worker Nodes** 구조의 분산 런타임을 구축합니다.
GCS(Global Control Store)는 분산 컴퓨팅의 모든 메타데이터(노드 상태, 작업 대기열, Task Lineage, 체크포인트 경로)를 유지하는 중앙 동기화 계층으로 동작하며, Head Node 내에 인메모리 스토어로 내장됩니다.

```mermaid
graph TD
    subgraph Head Node
        Scheduler["Q-Learning 스케줄러 (6D + OS 이론)"] <--> GCS["GCS (Global Control Store)"]
        GCS <-->|Save/Load 영속화| GCSDisk["data/gcs_state.json"]
        ScaleManager["Dynamic Worker Manager"] -->|Docker SDK cGroup & NVIDIA MPS 동적 주입| Containers["Worker Containers (cgroup & MPS 격리)"]
    end
    
    subgraph Worker Nodes
        W1["Worker-1 (On-Demand)"] <-->|gRPC & Heartbeat| Scheduler
        W2["Worker-2~N (Spot-A / Spot-B, MAX_SPOT_SCALE 동적 확장)"] <-->|gRPC & Heartbeat| Scheduler
    end
    
    GCS <-->|Lineage & 상태 공유| Scheduler
```

---

## 2. gRPC 기반 고성능 통신 인터페이스 및 프로토콜 규격

### 가. 프로토콜 타임아웃 및 메트릭 전송 수치 정의
1.  **Heartbeat 전송 주기**: 모든 활성 Worker는 **5.0초** 간격(`common/config.py:DEFAULT_HEARTBEAT_INTERVAL = 5.0`)으로 Head Node에 자신의 CPU/Memory 자원 사용률을 포함한 상태 패킷을 송신합니다.
2.  **생존 유실 판정 임계치 (Heartbeat Timeout)**: Head Node가 특정 Worker로부터 **3.0초** 동안 Heartbeat를 수신하지 못하면, 해당 노드를 `DEAD` 상태로 간주하고 장애 복구 프로토콜을 수행합니다. 단, `worker-1`(`on_demand`) 고정 노드는 DEAD 판정 대상에서 영구 제외합니다.
    *   ⚠️ **설계상 유의점 (코드 기준)**: 송신 주기(5.0초)가 판정 임계치(3.0초)보다 길어, 실제 생존한 스팟 노드도 순간적으로 DEAD로 오탐될 여지가 있습니다. 이는 스팟 Eviction을 3초 이내로 대단히 기민하게 감지하고 장애 자가 복구를 즉시 구동하기 위해 의도된 트레이드오프 설계 방식입니다.
3.  **태스크 할당 및 수거 지연**: gRPC 호출(`AssignTask`/`GetTaskStatus`)에는 명시적 deadline을 설정하지 않으며, 대신 스케줄러의 **0.2초 고속 의사결정 루프(High-Frequency Scheduling)**와 매 틱(5틱마다 1회인 1초 주기)마다 GCS 워커 레지스트리 생존 재확인으로 유실을 감지합니다.

### 나. Protobuf 인터페이스 규격 (`proto/babyray.proto`)
```protobuf
syntax = "proto3";
package babyray;

service BabyRayService {
  rpc RegisterWorker (RegisterRequest) returns (RegisterResponse);
  rpc SendHeartbeat (HeartbeatRequest) returns (HeartbeatResponse);
  rpc AssignTask (AssignRequest) returns (AssignResponse);
  rpc GetTaskStatus (StatusRequest) returns (StatusResponse);
  rpc ResizeResources (ResizeRequest) returns (ResizeResponse);
}
```

---

## 3. 노드별 물리 자원 격리 스펙 및 요금 모델 (`common/cost_model.yaml`)

| 노드 타입 | `cpu_limit` | `memory_limit_mb` | `cost_per_hour` | `gpu_scale_factor` | `preemption_probability` |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **On-Demand** (기본 코어 노드) | 2.0 | 2048 | **$7.10/hr** | 1.0 | 0.0 |
| **Spot-A** (성능 지향 가속 노드) | 1.0 | 1024 | **$2.20/hr** | 0.6 | **0.50** |
| **Spot-B** (안정 지향 경량 노드) | 0.5 | 512 | **$0.90/hr** | **0.5** | 0.10 |

*   On-Demand는 가장 안정적이며 preemption이 없고, Spot-A는 60% GPU 성능 격리를 지원하는 고요금/고선점 위험 노드이며, Spot-B는 가장 저렴하고 회수율이 낮아 안정적인 극가성비 최경량 노드입니다.
*   초기 가상 예산은 **$1.5**(`head/state.py:INITIAL_VIRTUAL_BUDGET`)로 설정되어 있습니다. (실제 벤치마크 시나리오 상에서 비용 축의 예산 고갈을 체감할 수 있도록 현실화된 단가)
*   **cGroup & MPS 동적 로드**: 컨테이너 실제 기동 시 `cluster_manager.py`의 `scale_out_worker()`는 하드코딩 대신 `_load_node_config()` 헬퍼를 통해 `common/cost_model.yaml` 명세를 **동적으로 로드**하여 `nano_cpus` 및 `mem_limit`, 그리고 NVIDIA MPS 스레드 제한(`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE` = `gpu_scale_factor * 100`)을 컨테이너 생성 시 동적 주입합니다.

---

## 4. 3대 스케줄러 메커니즘 특성 비교

| 비교 항목 | Static 스케줄러 모드 | Dynamic 스케줄러 모드 | Q-Learning (6D + OS 이론) 스케줄러 모드 |
| :--- | :--- | :--- | :--- |
| **의사결정 방식** | 큐 대기 크기 기준 정적 임계치 룰 | 노드 평균 자원(CPU/MEM) 부하 임계치 룰 | 6차원 상태 인지 및 행동 정책 기계 학습 |
| **스케일 아웃 조건** | 큐 길이 $\ge 2$ → Spot-A 1대, $\ge 6$ → 2대 | (평균 CPU/MEM $\gt 70\%$) 또는 큐 $\ge 3$ → 1대, 큐 $\ge 8$ → 2대 | Action 4/5 선택 시 기동(큐 $\ge 6$이면 2대). 예산·요금·위험구간 등에 따라 학습된 정책으로 결정 |
| **스케일 인 조건** | 큐가 비고 유휴 타이머 $\ge 3.0$초 유지 | 큐가 비고 평균 CPU/MEM $\lt 20\%$ 가 $3.0$초 유지 | 유휴(IDLE) 스팟 노드가 감지된 상태가 $3.0$초 지속(Spot-A 우선 회수) |
| **자원 효율성** | 낮음 (큐 크기만 보고 확장하므로 자원 낭비) | 보통 (실시간 자원 부하를 추적하여 분산함) | **높음** (모형의 성격에 맞춰 하드웨어 친화적 격리 배정) |
| **Starvation 해결** | 없음 (FIFO 순차 처리로 인한 지연) | 없음 | **있음 (SLA 지연 페널티 적용)**: 마감 초과 시 초당 −5.0 누적 감점 |
| **선두 차단(HOL) 해결**| 없음 | 없음 | **있음 (OS Backfilling 결합)**: 후순위 태스크 우회 배정 |
| **한계 및 단점** | 워크로드 폭증 시 유연한 대처 불가 | 일시적인 부하 요동에 따른 노드 플래핑(Flapping) | 학습 수렴 전까지 탐험(Exploration) 오버헤드 존재 |
| **자원 간섭 회피** | 없음 | **있음**: CPU $\ge 80\%$ 또는 RAM $\ge 75\%$ 초과 노드 배정 배제 | **있음**: 6차원 상태 공간(`a_mix`) 및 동거 경합 모델 연동 |

> **현재 기본 구동 모드**: `head/state.py:SCHEDULER_MODE = "q_learning"`. Q-Learning 모드는 사전 학습(`head/q_learning/pretrain.py`)으로 수렴시킨 Q-Table을 로드하여 선택적으로 구동합니다. 세 모드 모두 공통 자원 가드(§3)와 Backfilling/장애 복구(§6)를 공유합니다.

---

## 5. 6차원 상태 공간(State Space) 및 OS 스케줄링 이론 접목

Q-Learning 에이전트의 상태 변별력을 극대화하여 실제 시스템상의 병목 현상을 방지하도록 수학 모델을 6차원으로 고도화합니다. 모든 스케줄링 상태는 [state_features.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/q_learning/state_features.py)에서 유일하고 동기화된 방식으로 산출됩니다.

### 가. 6차원 상태 공간 공식 정의

에이전트가 참조하는 상태는 아래 6개 이산 축의 튜플입니다.

$$State = (q\_bucket,\; head\_model,\; a\_mix,\; sla\_bucket,\; danger\_phase,\; budget\_level)$$

1.  **`q_bucket` (대기열 적체 깊이)** ∈ `{0, 1, 2, 3}`: 큐의 태스크 적체량에 따른 버킷 분류.
    *   `0`: 빈 큐 (적체량 0)
    *   `1`: 경적체 (적체량 1 ~ 3)
    *   `2`: 중적체 (적체량 4 ~ 7)
    *   `3`: 과적체 (적체량 8 이상) ➔ 대기열 캐스케이드(Cascade) 폭발 방지 지표
2.  **`head_model` (선두 실행가능 태스크 성격)** ∈ `{0, 1}`: GCS 큐 최선두에 대기 중인(의존성이 충족된) 태스크의 성격 분류.
    *   `0`: 연산 집약형 (CNN, MERGE 또는 태스크 없음)
    *   `1`: 메모리 집약형 (RNN, LSTM) ➔ 노드별 용량 초과 OOM 회피 라우팅용
3.  **`a_mix` (유휴 노드 활성 비트맵)** ∈ `{0 … 7}`: 현재 IDLE 상태로 배정이 가용한 노드 풀의 조합을 3비트로 인코딩.
    *   $$a\_mix = (\text{on\_demand idle}) \cdot 1 + (\text{spot\_a idle}) \cdot 2 + (\text{spot\_b idle}) \cdot 4$$
4.  **`sla_bucket` (SLA 마감 완급)** ∈ `{0, 1, 2}`: 선두 태스크의 마감 기한까지 남은 시간(`time_left = deadline - now`) 기준 버킷.
    *   `0`: 여유 (30.0초 초과, `SLA_MED_SEC` 초과)
    *   `1`: 중간 (10.0초 초과 ~ 30.0초 이하)
    *   `2`: 임박 (10.0초 이하, `SLA_TIGHT_SEC` 이하) ➔ Starvation 방어용 강제 마스킹 트리거
5.  **`danger_phase` (스팟 회수 위험구간 여부)** ∈ `{0, 1}`: 30초의 회수 위험 주기 중 현재가 강제 선점 위험구간에 진입해 있는가에 대한 플래그.
    *   `0`: 안전 구간 (30초 중 뒤 20초)
    *   `1`: 위험 구간 (30초 중 앞 10초) ➔ 스팟 회수 룰렛 타이밍 회피 지표
6.  **`budget_level` (잔여 가상 예산 수준)** ∈ `{0, 1, 2}`: 가상 예산의 잔여 수준에 따른 레벨화.
    *   `0`: 예산 위험 ($0.7 미만, `BUDGET_CRITICAL` 미만)
    *   `1`: 예산 낮음 ($3.0 미만, `BUDGET_LOW` 미만)
    *   `2`: 예산 여유 ($3.0 이상)

### 나. 행동 공간(Action Space) 정의 및 행동 마스킹 (Action Masking)

Q-Learning 에이전트는 다음 6개 행동 중 하나를 선택합니다.

| Action | 의미 | 비고 |
| :--- | :--- | :--- |
| `0` | ASSIGN_ON_DEMAND | On-Demand 노드에 배정 |
| `1` | ASSIGN_SPOT_A | Spot-A 노드에 배정 |
| `2` | ASSIGN_SPOT_B | Spot-B 노드에 배정 |
| `3` | HOLD | 배정 보류(대기) |
| `4` | SCALE_OUT_SPOT_A | Spot-A 스케일아웃 (큐 $\ge 6$이면 2대) |
| `5` | SCALE_OUT_SPOT_B | Spot-B 스케일아웃 (큐 $\ge 6$이면 2대) |

```mermaid
graph TD
    State["현재 상태 관측 (6D)"] --> Mask["행동 마스킹 필터"]
    Mask -->|예산 고갈| MaskBudget["0, 4, 5 제외"]
    Mask -->|호스트 RAM > 85%| MaskHost["4, 5 제외"]
    Mask -->|SLA 임박 & 유휴 노드| MaskSLA["3 (HOLD) 제외"]
    Mask -->|컨테이너 LAUNCHING 중| MaskCold["4, 5 제외"]
    
    MaskBudget & MaskHost & MaskSLA & MaskCold --> Selected["가용한 행동 풀 (Available Actions)"]
    Selected --> Policy{"Epsilon-Greedy 의사결정"}
    Policy -->|탐험 (Epsilon)| RandomAct["임의 행동 선택"]
    Policy -->|활용 (Greedy)| MaxQAct["최대 Q-value 행동 선택"]
```

---

## 6. 강화학습 보상 설계 및 OS 스케줄링 이론 접목

### 가. 지연 보상(Delayed Reward) 및 가중치 상수 정의
성급한 즉시 보상 대신, 태스크가 완전히 완료(Success)되거나 실패(Evicted/OOM)하는 시점에 해당 배정의 상태-행동 쌍에 보상을 소급 귀속하는 **지연 보상(Delayed Reward Credit Assignment) 경로**를 활성화합니다. ASSIGN(0,1,2) 행동의 지연 보상은 완료 스레드에서 수행하는 반면, 그 외 행동(HOLD, SCALE_OUT 등)은 **`head/q_learning/reward_policy.py` 단일 산출 모듈**을 공유하여 학습 오차를 방지합니다.

$$Reward = R_{success} - C_{cost} - P_{makespan} - P_{delay} - P_{evicted} + R_{co\text{-}sched}$$

| 항 | 가중치 상수 | 설명 |
| :--- | :--- | :--- |
| $R_{success}$ | `SUCCESS_REWARD = 20.0` | 태스크 성공 완료 시 부여 (무행동 함정 방지용 상향) |
| $C_{cost}$ | `COST_WEIGHT = 1000.0` | 초당 환산 비용 체감 감점을 위해 $1000.0 \times (cost \times time / 3600)$ 차감 |
| $P_{makespan}$ | `MAKESPAN_WEIGHT = 0.5` | 마감 초과 여부와 무관하게 '느림' 자체에 대가 부과 ($0.5 \times execution\_time$) |
| $P_{delay}$ | `DELAY_PENALTY_WEIGHT = 5.0` | 마감 기한 초과분에 대해 **초당 −5.0**의 지연 페널티 부과 |
| $P_{evicted}$ | `EVICTION_PENALTY = 25.0` | 스팟 노드가 강제 회수(Eviction)되어 실패한 경우 부과되는 벌점 |
| $R_{co\text{-}sched}$ | 융합(상보 자원) 시 **+0.15** / 경합(동일 자원) 시 **−0.20** | 이종 모형 동거 시의 조화도 추가/감점 |

### 나. 비-ASSIGN 행동의 보상 통합 산출 (`reward_policy.py`)
1.  **HOLD 보상 (`hold_reward`)**:
    *   무행동 방지를 위해 큐 길이와 태스크별 마감 초과 시간에 따라 동적 벌점을 부여합니다.
        $$Reward_{hold} = 1.0 - 0.5 \times \text{q\_len} - \sum \left(\text{overdue\_seconds} \times \text{delay\_penalty\_weight} \times 0.2\right)$$
2.  **SCALE_OUT 보상 (`scale_reward`)**:
    *   물리 자원 고갈 또는 스팟 공급 부족(OutOfCapacity 확률 30% 모사) 등으로 가동 실패 시 강벌점 `-10.0`을 즉시 부과합니다.
    *   성공 시에는 아래와 같이 비용 국면(`cost_level` 1 = 요금 >$9/hr) 및 긴급도(`urgent` = SLA 기한 <=10초)에 연동해 보상합니다.
        *   **Spot-A (Action 4)**: $(4.0 \text{ if urgent else } -1.5) - 3.5 \quad [+\,3.0 \text{ if cost\_level=0 else } -3.0]$
        *   **Spot-B (Action 5)**: $(1.0 \text{ if urgent else } 0.0) - 2.0 \quad [+\,1.0 \text{ if cost\_level=0 else } -2.0]$
3.  **불가능 배정 보상 (`impossible_assign_reward`)**:
    *   가용 워커가 없는데 ASSIGN을 억지로 낸 경우, 학습에 노이즈를 주지 않도록 **`0.0` (중립)**으로 보상합니다.

### 다. 운영체제(OS) 스케줄링 기법의 결합
1.  **Backfilling (비순차 스케줄링) 을 통한 HOL Blocking 극복**
    *   최선두 태스크 배정이 보류될 경우, 큐 내부를 후방 탐색하여 현재 비어 있는 Spot-A 노드 스펙에 딱 맞는 CNN 작업을 선제 배정하여 클러스터 가동률을 극대화합니다.

---

## 7. GCS & Task Lineage 기반 장애 자가 복구 (Fault Tolerance)

스팟 노드가 중단될 때 중간 연산 결과를 보존하고 복구하는 계보 관리 설계입니다.

```mermaid
sequenceDiagram
    participant Worker
    participant Head_GCS as Head Node (GCS)
    participant ClusterManager as Docker Cluster Manager
    
    Note over Worker, Head_GCS: 정상 하트비트 통신 중 (5.0초 간격)
    Worker->>Head_GCS: Heartbeat (Status: OK)
    Note over Worker, Head_GCS: 스팟 노드 선점 회수 (Eviction) 또는 OOM 발생
    Note over Head_GCS: 3.0초 이상 하트비트 미수신 감지 (DEAD 판정)
    
    rect rgb(240, 200, 200)
        Head_GCS->>Head_GCS: check_and_cleanup_dead_workers()
        Head_GCS->>Head_GCS: Task Lineage 분석 (FAILED 표기)
        Head_GCS->>Head_GCS: 태스크 대기열 복구 재큐잉 (is_recovered_subtask: True, deadline = now + 20s)
    end
    
    Head_GCS->>ClusterManager: scale_out_worker("spot_a") 자동 연동
    ClusterManager->>Worker: 새 Spot Worker 컨테이너 기동
    Worker->>Head_GCS: final_*.pt 캐시 확인 (Skip-Execution) 또는 최신 체크포인트 로드 (이어서 재개)
```

### 가. GCS 상태 영속 체크포인팅 (State Persistence)
*   `head/state.py`의 스레드 안전 함수 `save_gcs_state()` / `load_gcs_state()`로 `data/gcs_state.json`에 상태를 영속화합니다. 
*   마스터 재기동 시 데드라인(`deadline`)이 과거 시간으로 고착되는 것을 방지하기 위해 **현재 기동 시간 기준으로 타임아웃을 강제 시프트**하는 보정 로직을 포함합니다.

### 나. Skip-Execution & 최신 체크포인트 이어서 재개 (Re-execution)
*   **Skip-Execution**: Map 서브태스크 복구 시 최종 결과 파일 `data/final_{sub_task_id}.pt`가 이미 존재하면 연산을 건너뛰어 비용을 절약합니다.
*   **이어서 재개**: 중간 체크포인트 `data/checkpoint_{sub_task_id}_epoch_{ep}.pt`가 검출되면, 에포크를 내림차순 스캔해 **최신 에포크 가중치를 로드하고 남은 에포크만큼만** 학습을 재개합니다.

### 다. DEAD 워커 연쇄 복구 (Cascaded Recovery) 및 자동 스케일아웃
*   Heartbeat 3.0초 미수신으로 `DEAD` 판정 시, 해당 노드가 수행 중이던 서브태스크의 Lineage를 `FAILED`로 표기하고, 복구 대상 서브태스크를 `is_recovered_subtask: True` · **`deadline = now + 20.0초`**와 함께 **대기열 최하단(맨 뒤, `append`)**으로 재큐잉합니다. 재큐잉 즉시 대체 자원을 확보하기 위해 스팟 노드를 자동 스케일아웃 연동합니다.
    *   ClusterManager->>Worker: 새 Spot Worker 컨테이너 기동
    *   Worker->>Head_GCS: final_*.pt 캐시 확인 (Skip-Execution) 또는 최신 체크포인트 로드 (이어서 재개)

---

## 8. 3대 ML 워크로드 성능 시뮬레이션 및 격리 성능 측정

실제 분산 클러스터 환경(`PyTorchActualRunner`)에서는 인위적인 `sleep` 패딩을 배제하고, 하드웨어 수준에서 이기종 성능 편차를 재현하기 위해 NVIDIA MPS의 물리 CUDA 스레드 할당 격리 비율을 환경변수로 주입하여 **물리 연산 속도를 직접 실측**합니다.
*   **물리 GPU 격리비율 (`CUDA_MPS_ACTIVE_THREAD_PERCENTAGE`)**: `gpu_scale_factor * 100` (%)
    *   On-Demand: 100% (Scale 1.0)
    *   Spot-A: 60% (Scale 0.6)
    *   Spot-B: 50% (Scale 0.5)

PyTorch 미지원 환경의 CPU 모사 모드(`CPUDummySimulationRunner`)에 한해서만 모델 고유 부하에 맞춰 에포크당 0.02초(CNN), 0.08초(LSTM), 0.05초(RNN)의 시차를 모사합니다.
*   **OOM 시뮬레이션 동거 경합 모델**:
    LSTM 및 RNN 태스크는 노드 타입에 따라 기본 OOM 확률(Spot-B 20%, Spot-A 8%, On-Demand 2%)에 더해, **동일 물리 노드 내 동거 중인 메모리 집약형 태스크 개수당 8%씩 가산(최대 60% 상한)**되는 실전적 경합 확률을 모사하여 무분별한 스케줄링을 제어합니다.

---

## 9. 실시간 비용 청구 모델 (Continuous VM Billing)

비용-SLA 트레이드오프를 학습시켜 Q-Learning 에이전트가 적시 스케일인을 배우도록 하기 위한 과금 모델입니다.

*   **실시간 비용 청구**: 가상 예산 고갈의 위기감을 체감하고 에이전트가 적시 스케일인을 유도하도록 하기 위해, AWS/GCP처럼 **가동 중인 모든 워커 노드의 시간당 요금을 초 단위로 환산하여 스케줄러 핵심 루프의 매 주기(0.2초)마다 가상 예산에서 실시간 차감**합니다.
    $$\text{Virtual Budget Decrement} = \frac{\text{Cost Per Hour}}{3600.0} \times 0.2$$
*   **정합화 완료**: `scheduler_daemon.py` 의 메인 루프에 실시간 차감 로직이 완전히 정합화 적용되었으며, 대시보드의 예산 표기 및 차감도 실제 초기 가상 예산($1.5 기본값)과 연동하여 동기화 가동 중입니다.

---

## 10. WE-MEET 고도화 3대 로드맵 (출장 주간 연구 및 고도화 과제)

본 섹션은 WE-MEET 아키텍처의 학술적/실무적 깊이를 극대화하기 위해 출장 주간 동안 수립 및 보강할 3대 고도화 기술 과제를 정의합니다.

### 가. Ray 논문 사상 구현 (Head-free Data Flow)
* **목표**: 마스터(Head) 노드의 중간 데이터 전송/병합 오버헤드와 Bottleneck(병목)을 완전히 배제하고, Ray의 설계 철학을 준수합니다.
* **상세 방안**:
  * Head 노드는 오직 gRPC 메타데이터 및 DAG 실행/태스크 할당 흐름(Control Plane)만 제어하도록 차단합니다.
  * AI 가중치(weights) 및 데이터셋(`.pt`)의 실질적인 수집, 병합, 전달 등은 Head 노드를 경유하지 않고, 워커 노드 간 직접 gRPC 연결(P2P) 또는 NFS(공유 분산 저장소)를 통해 병렬로 통신(Data Plane)하도록 유도합니다.

### 나. 모델 특성별 cGroup 리소스 파티셔닝 (CNN, RNN, LSTM 융합)
* **목표**: 비전 모형(CNN)과 시퀀스/텍스트 모형(RNN, LSTM)이 요구하는 물리적 하드웨어 접근 패턴 및 메모리 팽창 특성이 다름에 따라, 격리 리소스 설정을 동적으로 다르게 가져갑니다.
* **상세 방안**:
  * **비전 특화(CNN) cGroup 프로파일**: CPU 연산 점유는 최소화하되 GPU CUDA 패스스루 한도를 최대로 유지하여 가속 처리에 최적화합니다.
  * **텍스트 특화(RNN/LSTM) cGroup 프로파일**: 그라디언트 및 시퀀스 처리에 따른 OOM을 미연에 방지하기 위해 CPU 연산 가중치와 RAM 제한(limits)을 상대적으로 상향 적용하되 Swap 메모리를 예비용으로 바인딩합니다.
  * **융합형 스케줄링**: 대기열의 태스크 유형을 식별해 이에 맞는 최적의 cGroup 리소스 프로파일을 바인딩하고 있는 워커 풀로 적격 배정하는 융합 알고리즘을 도입합니다.

### 다. 고가용성(HA) 및 탄력성(Scale-In/Out) 안정성 강화
* **목표**: 노드 오프라인 장애 시 완벽한 계보 복구 및 자동 자원 회수/증설(Auto-scaling)의 플래핑(Flapping) 현상을 제어합니다.
* **상세 방안**:
  * **자가 치유 계보(Fault-Tolerance Lineage)**: 워커가 다운되었을 때 이미 완료된 캐시 데이터를 보존한 채, 유실된 서브 DAG 트리의 미완료 연산만 복구하여 인접 노드에 점진적으로 재배정하는 Lineage 기반 자가 치유를 고도화합니다.
  * **탄력성 가드(Auto-scaling Safeguard)**: 노드가 잦은 주기로 생성/삭제(Flapping)되어 발생하는 호스트 부하를 막기 위해 기동 후 최소 유지 시간(Cooldown Time)을 부여하고, 호스트 VM 내부의 실질적 가용 자원 잔여량(`wsl free -b`)을 지속 추적하여 리소스 고갈 상황에서는 선제적으로 스케일 아웃을 보류하는 안전 안전장치를 적용합니다.

---

## 11. 분산 P2P 통신의 성능 오버헤드 및 극복 방향성

현재의 파일 공유 볼륨 기반 통신은 텐서 크기가 커질수록 극심한 디스크 I/O 병목을 초래하므로, 상용 분산 학습 환경(Ray, PyTorch DDP)에서는 공유 메모리(Plasma Object Store) 및 NCCL 기반 P2P 직접 통신(All-Reduce)을 활용합니다.
P2P 전송 방식은 통신 도중 노드가 탈퇴하거나 유실되었을 때 링 구조가 붕괴되는 치명적인 복잡성을 수반하지만, 이는 Rendezvous 합의 백엔드(etcd 등)를 통해 노드 멤버십 변화를 감지하고 가상 Rank를 실시간 재부팅하는 링 재구성(Re-rendezvous)과 Rank 0 기준의 가중치 Broadcast 강제 동기화 기법을 통해 안정적으로 극복됩니다.
