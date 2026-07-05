# WE-MEET 패치노트 (2026.06.30)

본 패치노트는 이기종 가상 클러스터 기반 ML 분산 학습 제어 엔진인 **WE-MEET**의 모듈 오류 디버깅, 로그 대시보드 연동, Q-Learning 4차원 상태 공간 확장, PyTorch 분할/결합(FedAvg) 런타임, 그리고 Ray 사상(Shared Memory & P2P) 실증을 위한 전방위 패치 내역을 기술합니다.

---

## 🛠️ 1. 버그 수정 및 로그 대시보드 연동

* **스케줄러 임포트 오류 (ImportError) 완전 해결**:
  - `head/scheduler.py` 내의 레거시 패키지 임포트 구조를 실제 변경된 서브모듈 구조인 `head.scheduler.static`, `head.scheduler.dynamic`, `head.q_learning.scheduler`, `head.q_learning.agent`, `head.state`, `head.cluster_manager` 등으로 올바르게 패치하여 임포트 및 컴파일 실패를 해결했습니다.
* **좀비 컨테이너 클리너 대시보드 GUI 연동**:
  - `head/cluster_manager.py` 내의 `cleanup_zombie_containers()` 함수 내 핵심 로그 출력 코드들(`print`)을 전부 `dashboard.log_event`로 교체하였습니다.
  - 마스터 서버 부팅 시 백그라운드로 작동하는 좀비 컨테이너의 소거 진행 상황이 대시보드 웹 GUI(`http://localhost:8080`)의 실시간 콘솔창에 정상적으로 동기화 출력되도록 연동하였습니다.

---

## 🧠 2. Q-Learning 4차원 상태 공간 ($State = (T_{profile}, W_{active}, P_{spot}, B_{level})$) 확장

* **상태 세부 모델 이식**:
  - `T_profile` (대기열 워크로드 프로파일): 대기열 내부의 CNN 대 LSTM/RNN 비율을 0 또는 1로 분류.
  - `W_active` (IDLE 상태인 가용 노드 조합): On-Demand와 Spot-A 노드 중 실제 IDLE인 상태의 가동 조합 비트맵 추출.
  - `P_spot` (실시간 가상 요금제 변동 및 중단 위험도): 시뮬레이터 가격 변동성에 맞추어 안정기(0)와 폭등기(1)가 주기적으로 변동되도록 모사.
  - `B_level` (가상 예산 잔여 수준): 잔여 예산 비율 상태(0: 고갈, 1: 경고, 2: 풍족) 유지.
* **적용 파일**: 
  - [agent.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/q_learning/agent.py) (`_state_to_str` 4차원 직렬화 수정)
  - [scheduler.py](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/q_learning/scheduler.py) (스케줄링 상태 판단 주입)
  - [core.py (scheduler/core)](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/scheduler/core.py) & [scheduler.py (head)](file:///c:/Users/win/Desktop/클라우드  WE-MEET 프로젝트/WE-MEET/head/scheduler.py) (`next_state` 계산 로직 동기화)

---

## ⚡ 3. PyTorch 가중치 보존 및 FedAvg 가중치 평균 병합 (Reduce Phase) 구현

* **에포크별 모델 인스턴스 보존 학습 (Continual Learning)**:
  - `worker/gpu_simulator.py` 내의 1에포크당 새 모델을 생성하던 더미 구조를 탈피하여, 단 한 번 모델과 옵티마이저를 초기화하고 매 에포크 끝에 `torch.save()`로 체크포인트(`.pt`)를 저장하도록 수정했습니다.
  - 작업 기동 시 `dataset_path` 파라미터가 이전 완료된 체크포인트 경로(`.pt`)일 경우 `model.load_state_dict()`로 상태를 적재하고 이어서 훈련하는 상태 상속 루틴을 완비했습니다.
* **진짜 가중치 평균 병합 (Federated Averaging)**:
  - `dataset_path`에 `merge:` 접두사로 여러 Map 가중치 파일 경로가 매핑될 경우, 메모리에 텐서들을 직접 적재해 평균 병합(FedAvg) 가중치 파일(`final_weights.pt`)을 저장하는 실제 PyTorch 연산 코드를 완비했습니다.
  - **BatchNorm 정수형 텐서 예외 처리**: `num_batches_tracked` 등 배치 정규화 과정의 정수형 카운터 텐서 평균 연산 에러(`could not infer output dtype`)를 방지하는 예외 필터링 처리를 적용했습니다.
* **생성자 바인딩**: `worker/worker.py`에서 `PyTorchTaskRunner` 호출 시 빠져 있던 `request.dataset_path` 인자를 바인딩하도록 수정했습니다.

---

## 🌐 4. Ray 사상 (Shared Memory & P2P) 및 클라우드 제약 실증

* **공유 메모리(Plasma Store) 모사**:
  - Docker Compose 볼륨 공유 설정을 활용해 모든 컨테이너가 로컬 `./data` 디렉토리를 공유하게 함으로써, 가중치 물리 파일의 네트워크 복사 이동 오버헤드가 제로가 되는 **제로 카피 형태의 분산 공유 메모리 구조**를 실증했습니다.
* **P2P 분산 데이터 전송 (Head-free Data Flow)**:
  - Head 노드는 오직 제어용 메타데이터(`merge:data/...pt`)만 gRPC로 던져주며 (Control Plane), 가중치를 병합하는 `Reduce` 워커는 공유 디스크 상에서 가중치들을 스스로 읽어 들여 병합 (Data Plane)하게 만들어 Head 대역폭 병목을 완전히 격리하였습니다.
* **스팟 공급 거절 및 실시간 Eviction 모니터링 데몬**:
  - `head/cluster_manager.py`에 **30%의 확률로 자원 부족에 의한 스팟 생성 거절 (OutOfCapacity)** 로직을 추가했습니다.
  - 백그라운드에서 실시간 요금제 위험도 P_spot 수준에 맞춰 주기적으로 기동 중인 스팟 노드를 대상(안정기 5%, 폭등기 15%)으로 강제 정지/제거하는 **Eviction 모니터링 데몬 스레드**를 탑재했습니다.

---

## 🧪 5. 검증 결과

* **유닛 테스트 통과**:
  - `scratch/test_pytorch_operations.py`를 구동하여 PyTorch 가중치의 저장, 적재, BatchNorm 텐서 예외 처리 필터링이 가미된 FedAvg 병합이 무결하게 성공함을 입증했습니다.
* **3대 스케줄러 벤치마크 테스트 완료**:
  - `scratch/run_benchmark.py`를 실행하여 4차원 상태 모델과 쪼개기/결합 및 스팟 중단(Eviction) 상황에서 Static vs Dynamic vs Q-Learning 스케줄러가 에러 없이 작동하고, Q-Learning이 DYNAMIC 대비 우수한 예산 보존 수렴을 보임을 검증 완료하였습니다.
