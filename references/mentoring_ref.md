# WE-MEET 학술 레퍼런스 분석 및 극복 방안 리포트

본 문서는 WE-MEET 프로젝트의 학술적 토대가 된 3대 선행 연구 논문을 체계적으로 정리하고, 각 논문의 핵심 차용 기술, 한계점, 그리고 WE-MEET에서 이를 어떻게 극복 및 융합하는지 명세합니다.

---

## 1. Reference 1: Baby Ray (Stanford University)
*   **논문 제목**: *Baby Ray: Re-implementing a distributed Python runtime in Go*
*   **핵심 참고 내용**:
    *   Ray의 핵심 컴포넌트인 **GCS(Global Control Store)** 및 **Local/Global Scheduler** 분산 런타임을 Go와 gRPC로 축소 구현하여 실증함.
    *   **Locality-aware 스케줄링**: 데이터가 위치한 노드에 연산을 우선 배치하여 네트워크 전송 오버헤드를 제어함.
    *   **Task Reconstruction**: 노드 다운 시 GCS 내의 계보(Lineage) 그래프를 기반으로 유실된 태스크를 추적 및 재수행하여 결함 허용(Fault-Tolerance) 구현.
*   **발생 문제 및 WE-MEET의 극복 방안**:
    *   *문제점*: Baby Ray는 이기종 클러스터의 자원 한계선 제어(cGroup Limits)나 클라우드 가상 예산(Cost-aware) 개념이 배제된 채, 순수 기능적 복제에만 집중하여 자원 효율화 측면의 변별력이 약함.
    *   *WE-MEET의 극복*: Baby Ray의 제어(Control Plane) 흐름을 차용하되, 아래 오픈스택 cGroup 및 CASF의 비용 모델을 결합하여 **"자원 효율화 및 가상 요금 인지형 분산 스케줄러"**로 확장 진화시킴.

---

## 2. Reference 2: 오픈스택 가상머신 성능제어 (고려대학교)
*   **논문 제목**: *오픈스택 환경에서 성능 차등화 및 자원 효율성 향상을 위한 가상머신 성능제어 기법 분석*
*   **핵심 참고 내용**:
    *   리눅스 커널의 **cGroup 파라미터**(`cpu.shares`, `memory.limits_in_bytes`, `blkio.throttle`)를 조절하여 동일 물리 노드 내에 떠 있는 가상머신(또는 컨테이너)들의 하드웨어 자원을 격리하고 성능을 강제로 차등화함.
*   **발생 문제 및 WE-MEET의 극복 방안**:
    *   *문제점*: 가상머신의 사양은 고정적으로 제공되는 반면, 구동하는 AI 모형의 연산/메모리 집중도가 시시각각 변하므로 정적 할당 시 심각한 자원 낭비(Idle 잉여) 또는 부족(OOM)이 발생함.
    *   *WE-MEET의 극복*: CNN(연산 집중형), RNN(균형형), LSTM(메모리 임베딩 집중형) 등 **워크로드의 특성을 사전에 분류(Profiler)**하고, 스케줄링 매핑 결과에 맞춰 컨테이너의 cGroup 한계를 실시간 동적으로 조율하는 **동적 격리 파티셔닝**으로 승화시킴.

---

## 3. Reference 3: Cost-Aware ML Scheduling (2025 MRIE)
*   **논문 제목**: *Cost-Aware Scheduling of Machine Learning Workloads in Cloud Platforms*
*   **핵심 참고 내용**:
    *   스팟 인스턴스의 실시간 가격 변동 및 중단(Preemption) 위험이 높은 가변 클라우드 환경에서 ML 태스크의 **마감 기한(Deadline)**과 **예산 제한(Budget Constraint)** 조건 하에서 비용을 최소화하는 수식 정의.
    *   강화학습(Q-learning, DDPG)을 사용해 동적으로 변하는 클라우드 요금제 하에서 최적의 액션을 선택하도록 유도.
*   **발생 문제 및 WE-MEET의 극복 방안**:
    *   *문제점*: 기존 Q-Learning의 상태 공간(State)이 단순히 대기열과 요금 레벨에만 의존하여, CPU 연산 중심 작업과 GPU/메모리 중심 작업의 하드웨어 친화도 차이를 전혀 구별하지 못함. 이로 인해 단순 룰 기반(Dynamic) 분배 스케줄러와 강화학습 간 성능 차이가 전무했음.
    *   *WE-MEET의 극복*: 강화학습 에이전트의 상태 공간(State Space)에 **"대기열의 워크로드 자원 요구도 특성(Task Profile)"**과 **"클러스터 노드의 물리 리소스 가용 압박 지수"**를 상태 변수로 추가하는 **4차원 고도화 상태 공간**을 설계하여 실질적인 자원 친화도 기반 학습이 작동하도록 보완함.
