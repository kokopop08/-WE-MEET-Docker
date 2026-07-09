import time
import random # FailureSimulator 내부에서 사용 (직접 호출 제거됨)
import os
from abc import ABC, abstractmethod

# 표준 출력 버퍼 비우기 (Flush) 설정
# 백그라운드나 컨테이너 환경에서 로그가 즉시 출력되도록 보장합니다.
import builtins
_original_print = builtins.print
def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    _original_print(*args, **kwargs)
builtins.print = print

# 가상 OOM 시뮬레이션 플래그 (메모리 부족)
# worker.py가 동일 프로세스에서 import로 접근하는 구조이므로 common으로 이동 불가 - 여기에 유지
oom_simulated = False

# 장애 시뮬레이션 판단 로직 중앙화 모듈
from wemeet.simulation.failure_simulator import FailureSimulator

# 분리된 신경망 연산 및 추론 모듈 로드 (wemeet.workload.models)
from wemeet.workload.models import (
    get_task_by_type,
    HAS_TORCH,
    CNNModel,
    RNNModel,
    LSTMModel
)
#pytorch 라이브러리가 사용 가능한 경우 import
if HAS_TORCH:
    import torch 
    import torch.nn as nn
    import torch.optim as optim

# 더미 부하 모사는 dummy_load 모듈이 단일 책임으로 보유(실행기와 분리).
from wemeet.workload import dummy_load


# 1. 공통 인터페이스 정의
class TaskRunner(ABC):
    """
    작업 실행기 공통 추상 베이스 클래스(Interface)입니다.
    
    Attributes:
        task_id (str): 실행할 태스크 고유 ID.
        model_type (str): 신경망 모델 유형 ("CNN" / "RNN" / "LSTM").
        epochs (int): 학습 Epoch 횟수.
        worker_type (str): 워커 유형 ("on_demand" / "spot_a").
        dataset_path (str): 데이터셋 파일 경로 또는 MERGE 문자열.
        progress (float): 작업 진행률 (0.0 ~ 100.0 %).
        status (str): 작업 실행 상태 ("RUNNING" / "SUCCESS" / "FAILED").
        logs (list): 연산 진행 로그 목록.
        execution_time (float): 총 소요 수행 시간.
    """
    def __init__(self, task_id, model_type, epochs, worker_type, dataset_path=""):
        self.task_id = task_id # task-cnn-001
        self.model_type = model_type # CNN, RNN, LSTM
        self.epochs = epochs # 학습 횟수 (랜덤)
        self.worker_type = worker_type # on_demand, spot_a
        self.dataset_path = dataset_path # 데이터셋(Re-execution) / merge:경로1,경로2 (Federated Learning)
        self.progress = 0.0
        self.status = "RUNNING"
        self.logs = []
        self.execution_time = 0.0

    @abstractmethod
    def run(self):
        pass


# 2. 실제 PyTorch GPU/CPU 연산 담당 실행기
class PyTorchActualRunner(TaskRunner):
    """
    실제 PyTorch 연산을 돌리면서 GPU VRAM 가드 및 MPS 하드웨어 제어 하에 동작하는 실행기입니다.
    """
    def __init__(self, task_id, model_type, epochs, worker_type, dataset_path=""):
        super().__init__(task_id, model_type, epochs, worker_type, dataset_path)
        self.task = get_task_by_type(model_type) # 모델에 맞는 학습 로직의 객체
        
        # 실제 GPU 점유율: NVIDIA MPS에서 CUDA 스레드 제한 비율 (환경변수로 컨테이너 기동 시 주입됨)
        self.mps_percentage = int(os.environ.get("CUDA_MPS_ACTIVE_THREAD_PERCENTAGE", 100))
        # CUDA_MPS_ACTIVE_THREAD_PERCENTAGE의 환경 변수의 값을 가져옴

    def _check_oom_trigger(self):
        """
        OOM 예외 유입 조건 감지 및 가상 실패 처리.
        판단 로직은 FailureSimulator.check_oom()에 위임합니다.
        (OOM은 Eviction(Preemption)와 다름 - 태스크만 FAILED, 컨테이너는 살아있음)
        """
        if FailureSimulator.check_oom(self.model_type, self.task_id):
            print(f"\n[Worker Simulation] !!! 가상 OOM 장애 유입 감지 !!! (Task: {self.task_id})")
            global oom_simulated
            oom_simulated = True
            self.status = "FAILED"
            self.logs.append("OOM Exception simulated: cGroup memory limit exceeded.")
            print("[Worker Simulation] cGroup 메모리 제한 초과로 강제 실패 처리 완료.")
            return True
        return False

    def _handle_reduce_task(self, start_time):
        """REDUCE(FedAvg) 가중치 병합 및 교차 추론 검증"""
        # start_time => run에서 넘어옴 (작업의 초 소요시간 기록)
        print(f"[Worker Task] 가중치 FedAvg 병합 연산 수행: {self.task_id}")
        try:
            # merge:/app/data/a.pt,/app/data/b.pt -> ["", "/app/data/a.pt,/app/data/b.pt"]
            paths_str = self.dataset_path.split("merge:")[1]
            
            file_paths = [p.strip() for p in paths_str.split(",") if p.strip()]
            
            # 각 경로가 실제 디스크에 존재하는지 확인
            valid_paths = [p for p in file_paths if os.path.exists(p)]
            
            # 유효한 파일이 하나도 없는 경우 에러 처리
            if not valid_paths:
                raise FileNotFoundError(f"[FedAvg Error] 병합할 유효한 가중치 파일(.pt)이 디바이스상에 하나도 존재하지 않습니다. (요청 리스트: {file_paths})")
            
            # 3/4 대 병합 진행 -> 유실된 정보 파악 가능
            print(f"[Worker Task] 유효 파일 스캔 완료: {len(valid_paths)}/{len(file_paths)} 대 병합 진행")
            
            # 파일들로부터 가중치 state_dict 로드
            state_dicts = [torch.load(p, map_location="cpu") for p in valid_paths]
            # [{"conv1.weight": tensor, "fc.bias": tensor, ...}, {...}, ...] 형태의 딕셔너리 리스트
            averaged_sd = {}
            
            # 모델 레이어별 파라미터
            base_keys = state_dicts[0].keys()
            for key in base_keys:
                tensors = []
                for sd in state_dicts:
                    if key in sd:
                        tensors.append(sd[key])
                    #  같은 key의 텐서를 수집합니다. if key in sd로 해당 키가 없는 파일은 건너뜁니다
                    # 중간에 죽거나 파일이 손상되거나 epoch에 차이가 있을 수 있기 때문에
                
                if not tensors:
                    continue
                    
                if tensors[0].dtype in [torch.float16, torch.float32, torch.float64, torch.bfloat16]:
                    averaged_sd[key] = torch.stack(tensors).mean(dim=0)
                    # 벡터화 연산( 반복문은 key를 순환하는데에서만 사용)
                else:
                    averaged_sd[key] = tensors[0]
            
            os.makedirs("data", exist_ok=True)
            output_path = f"data/final_{self.task_id}.pt"
            torch.save(averaged_sd, output_path)
            
            # [FedAvg 교차 추론 검증]
            inferred_type = "CNN"
            # State dict의 텐서 키값 패턴 매칭을 통해 실제 모델 타입을 정확하게 판별합니다.
            if averaged_sd:
                # any = 하나라도 포함되어 있으면 참
                # "rnn."으로 시작하는 게 있으면 True
                if any(k.startswith("rnn.") for k in averaged_sd.keys()):
                    inferred_type = "RNN"
                elif any(k.startswith("lstm.") for k in averaged_sd.keys()):
                    inferred_type = "LSTM"
                    
                # 아키텍쳐를 모를경우 task id를 보고 판단
                elif "rnn" in self.task_id.lower():
                    inferred_type = "RNN"
                elif "lstm" in self.task_id.lower():
                    inferred_type = "LSTM"
            # 추론 테스트
            test_task = get_task_by_type(inferred_type)
            if test_task:
                test_model = test_task.get_model()
                test_model.load_state_dict(averaged_sd)
                inf_res = test_task.infer(test_model, "cpu")
                print(f"[Worker Task] [FedAvg Verification] {inf_res}")
                self.logs.append(f"[FedAvg Verification] {inf_res}")
            
            self.execution_time = time.time() - start_time
            self.status = "SUCCESS"
            self.progress = 100.0
            self.logs.append(f"Federated Averaging 병합 완료. 출력 파일: {output_path} (참여 노드 수: {len(valid_paths)})")
            print(f"[Worker Task] 가중치 FedAvg 병합 성공! 파일: {output_path} (참여 노드 수: {len(valid_paths)})")
        except Exception as e:
            self.status = "FAILED"
            self.logs.append(f"Federated Averaging 병합 에러: {str(e)}")
            print(f"[Worker Task] FedAvg 병합 실패: {e}")

    def _apply_vram_guard(self, device):
        """GPU VRAM 격리 Fraction 가드 설정"""
        if HAS_TORCH and device == "cuda":
            try:
                total_memory = torch.cuda.get_device_properties(0).total_memory
                vram_limits = {
                    "on_demand": 4096 * 1024 * 1024,
                    "spot_a": 2048 * 1024 * 1024,
                    "spot_b": 1024 * 1024 * 1024
                }
                limit_bytes = vram_limits.get(self.worker_type.lower(), 1024 * 1024 * 1024)
                fraction = min(1.0, limit_bytes / total_memory)
                
                torch.cuda.set_per_process_memory_fraction(fraction, 0)
                print(f"[GPU Guard] 물리 VRAM 제한 적용: {limit_bytes / (1024**2):.1f} MB (비율: {fraction:.4f})")
                self.logs.append(f"[GPU Guard] VRAM Limit applied: {limit_bytes / (1024**2):.1f} MB")
            except Exception as e:
                print(f"[GPU Guard 경고] VRAM 분할 격리 설정 실패: {e}")
                self.logs.append(f"[GPU Guard Warning] VRAM partition failed: {str(e)}")

    def _init_model_and_optimizer(self, device):
        """모델 및 옵티마이저 초기화 및 체크포인트 로드"""
        model = None
        optimizer = None
        criterion = None 
        
        if HAS_TORCH and self.task:
            try:
                model = self.task.get_model().to(device)
                optimizer = optim.SGD(model.parameters(), lr=0.01, momentum=0.9)
                criterion = self.task.get_criterion()
                
                if self.dataset_path and self.dataset_path.endswith(".pt") and os.path.exists(self.dataset_path):
                    print(f"[Worker Task] 이전 체크포인트 로드: {self.dataset_path}")
                    model.load_state_dict(torch.load(self.dataset_path, map_location=device))
                    self.logs.append(f"Loaded checkpoint state_dict from {self.dataset_path}")
            except Exception as e:
                print(f"[Worker Task] 모델 초기화 또는 체크포인트 로딩 중 에러: {e}")
                self.logs.append(f"Model init / checkpoint load failed: {str(e)}")
        return model, optimizer, criterion

    def _run_epoch_loop(self, model, optimizer, criterion, device):
        """에포크 학습 루프 수행"""
        for epoch in range(self.epochs):
            epoch_start = time.time()
            loss = 0.0
            
            if HAS_TORCH and model is not None and self.task:
                try:
                    loss = self.task.train_epoch(model, optimizer, criterion, device)
                    os.makedirs("data", exist_ok=True)
                    checkpoint_path = f"data/checkpoint_{self.task_id}_epoch_{epoch+1}.pt"
                    torch.save(model.state_dict(), checkpoint_path)
                except Exception as e:
                    print(f"[Worker Task] PyTorch 학습 연산 실패: {e}")
                    self.status = "FAILED"
                    self.logs.append(f"PyTorch training failed at epoch {epoch+1}: {str(e)}")
                    break # 실패 시 루프 탈출 (Dummy 호출 X, FAILED 처리)
            else:
                print("[Worker Task] PyTorch 미지원 또는 태스크 미설정 상태에서 PyTorchActualRunner 실행 시도됨.")
                self.status = "FAILED"
                self.logs.append("PyTorch is not available or task is not configured.")
                break

            if HAS_TORCH and device == "cuda":
                torch.cuda.synchronize()

            actual_time = time.time() - epoch_start
            epoch_total_time = actual_time
            log_line = f"Epoch {epoch+1}/{self.epochs} - Loss: {loss:.4f} - 연산시간: {actual_time:.4f}초"
            # 로그 출력량 최적화: 첫 에포크, 마지막 에포크, 혹은 5 에포크 주기로 기록
            if epoch == 0 or (epoch + 1) == self.epochs or (epoch + 1) % 5 == 0:
                self.logs.append(log_line)
                print(f"[Worker Task] {self.task_id} | {log_line}")
            self.progress = ((epoch + 1) / self.epochs) * 100.0

    def _save_final_model_and_inference(self, model, device):
        """최종 모델 가중치 저장 및 추론 검증"""
        if HAS_TORCH and model is not None and self.status != "FAILED" and self.task:
            try:
                os.makedirs("data", exist_ok=True)
                final_path = f"data/final_{self.task_id}.pt"
                torch.save(model.state_dict(), final_path)
                print(f"[Worker Task] 최종 모델 가중치 저장 성공: {final_path}")
                self.logs.append(f"Saved final weights to {final_path}")
                
                # [학습 모델 실 추론 검증]
                inf_res = self.task.infer(model, device)
                print(f"[Worker Task] {inf_res}")
                self.logs.append(inf_res)
            except Exception as e:
                print(f"[Worker Task] 최종 가중치 저장 실패: {e}")

    def run(self):
        """
        지정된 AI 모델 학습 연산을 수행합니다.
        가상 OOM 장애 유발 시나리오 및 NVIDIA MPS 기반 물리 GPU 성능 차별화를 적용합니다.
        """
        print(f"\n[Worker Task] 작업 시작 (실제): {self.task_id} (모델: {self.model_type}, 노드타입: {self.worker_type}, GPU MPS 점유율: {self.mps_percentage}%)")
        start_time = time.time()
        
        # 1. OOM 시뮬레이션 감지
        if self._check_oom_trigger():
            return
            
        # 2. FedAvg 결합 연산(Merge) 분기 처리
        if HAS_TORCH and self.dataset_path.startswith("merge:"):
            self._handle_reduce_task(start_time)
            return
            
        # 3. 구동 디바이스 판단 및 VRAM 가드 적용
        device = "cuda" if (HAS_TORCH and torch.cuda.is_available()) else "cpu"
        print(f"[Worker Task] 구동 디바이스: {device}")
        self._apply_vram_guard(device)
        
        # 4. 모델 및 옵티마이저 초기화
        model, optimizer, criterion = self._init_model_and_optimizer(device)
        
        # 5. 학습 루프 수행 (실패 시 FAILED 처리, Dummy 호출 X)
        self._run_epoch_loop(model, optimizer, criterion, device)
        
        # 6. 최종 모델 저장 및 추론 검증
        self._save_final_model_and_inference(model, device)
        
        self.execution_time = time.time() - start_time
        if self.status != "FAILED":
            self.status = "SUCCESS"
            
        # GPU VRAM 캐시 비우기 (Flush GPU cache)
        if HAS_TORCH:
            try:
                del model
                del optimizer
                if torch.cuda.is_available():
                     torch.cuda.empty_cache()
            except Exception:
                pass
        
        print(f"[Worker Task] 작업 완료: {self.task_id} (총 소요 시간: {self.execution_time:.2f}초)\n")


# 3. PyTorch가 없거나 시뮬레이션 전용인 가상 부하 실행기
class CPUDummySimulationRunner(TaskRunner):
    """
    PyTorch가 없거나 시뮬레이션 전용 환경일 때,
    기존 run_dummy_epoch 로직을 에포크별로 순회하며 CPU 및 메모리 점유 부하를 모사하는 실행기입니다.
    """
    def _check_oom_trigger(self):
        """
        OOM 예외 유입 조건 감지 및 가상 실패 처리.
        판단 로직은 FailureSimulator.check_oom()에 위임합니다.
        """
        if FailureSimulator.check_oom(self.model_type, self.task_id):
            print(f"\n[Worker Simulation] !!! 가상 OOM 장애 유입 감지 !!! (Task: {self.task_id})")
            global oom_simulated
            oom_simulated = True
            self.status = "FAILED"
            self.logs.append("OOM Exception simulated: cGroup memory limit exceeded.")
            print("[Worker Simulation] cGroup 메모리 제한 초과로 강제 실패 처리 완료.")
            return True
        return False

    def run(self):
        """
        기존 run_dummy_epoch 로직을 에포크별로 순회하며 
        CPU 루프를 돌리고 dummy_memory_holder에 메모리 점유 시뮬레이션을 수행합니다.
        """
        print(f"\n[Worker Task] 작업 시작 (모사): {self.task_id} (모델: {self.model_type}, 노드타입: {self.worker_type})")
        start_time = time.time()

        # 1. OOM 시뮬레이션 감지
        if self._check_oom_trigger():
            return

        # 2. 에포크별 dummy 연산 수행
        for epoch in range(self.epochs):
            epoch_start = time.time()
            loss = dummy_load.run_dummy_epoch(self.model_type)

            actual_time = time.time() - epoch_start
            log_line = f"Epoch {epoch+1}/{self.epochs} - Loss: {loss:.4f} - 연산시간: {actual_time:.4f}초"
            # 로그 출력량 최적화: 첫 에포크, 마지막 에포크, 혹은 5 에포크 주기로 기록
            if epoch == 0 or (epoch + 1) == self.epochs or (epoch + 1) % 5 == 0:
                self.logs.append(log_line)
                print(f"[Worker Task] {self.task_id} | {log_line}")
            self.progress = ((epoch + 1) / self.epochs) * 100.0

        self.execution_time = time.time() - start_time
        if self.status != "FAILED":
            self.status = "SUCCESS"

        # 메모리 시뮬레이션 공간 해제 (dummy_load 모듈 전역 홀더 직접 리셋)
        dummy_load.dummy_memory_holder = []

        print(f"[Worker Task] 작업 완료: {self.task_id} (총 소요 시간: {self.execution_time:.2f}초)\n")


# 4. 실행 환경(팩토리)에서 선택 주입
def get_task_runner(*args, **kwargs) -> TaskRunner:
    # 환경변수(예: RUN_MODE=SIMULATION) 또는 HAS_TORCH 여부에 따라 분기
    if os.environ.get("RUN_MODE") == "SIMULATION" or not HAS_TORCH:
        return CPUDummySimulationRunner(*args, **kwargs)
    return PyTorchActualRunner(*args, **kwargs)


# 하위 호환성을 유지하기 위한 팩토리 래퍼/에일리어스 설정
PyTorchTaskRunner = get_task_runner
