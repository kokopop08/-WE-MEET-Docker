import time
import random
import os

# 표준 출력 버퍼 비우기 (Flush) 설정
import builtins
_original_print = builtins.print
def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    _original_print(*args, **kwargs)
builtins.print = print

# 가상 OOM 시뮬레이션 플래그
oom_simulated = False 

# 분리된 신경망 연산 및 추론 모듈 로드 (worker.models)
from worker.models import (
    get_task_by_type,
    HAS_TORCH,
    CNNModel,
    RNNModel,
    LSTMModel
)
if HAS_TORCH:
    import torch 
    import torch.nn as nn
    import torch.optim as optim

# Fallback 더미 연산 설정
FALLBACK_SLEEP = 0.05         # Fallback 시 최소 대기 시간(초)

# 가상 메모리 홀더 (더미 메모리 점유 시뮬레이션용)
dummy_memory_holder = []


def run_dummy_epoch(model_type):
    """
    PyTorch가 없는 환경 또는 에러 발생 시, 
    모델 종류별로 차별화된 CPU 및 메모리 부하를 모사합니다.

    Args:
        model_type (str): 신경망 모델 유형 ("CNN" / "RNN" / "LSTM").

    Returns:
        float: 모사 손실 값 (0.0 ~ 1.0 사이의 float).
    """
    global dummy_memory_holder
    model_upper = model_type.upper()
    
    if model_upper == "CNN":
        # CNN: 연산 집중형 (높은 CPU 점유율 유도)
        cpu_loop_count = 1500000 
        dummy_sum = 0.0
        for i in range(cpu_loop_count):
            dummy_sum += (i * 0.0001) ** 0.5
        
        dummy_memory_holder = [] # 메모리는 거의 사용하지 않음
        time.sleep(0.02)
        return dummy_sum % 1.0

    elif model_upper == "LSTM":
        # LSTM: 메모리 점유형 (높은 메모리 사용률 유도)
        # 1GB 컨테이너 기준 약 12% (120MB) 임시 점유로 축소 조정 (OOM 방지)
        # float(8 bytes) * 15,000,000 = 약 120MB
        mem_element_count = 15000000 
        dummy_memory_holder = [0.123] * mem_element_count 
        
        cpu_loop_count = 50000
        dummy_sum = 0.0
        for i in range(cpu_loop_count):
            dummy_sum += i
            
        time.sleep(0.08)
        return dummy_sum % 1.0

    else:
        # RNN 및 기본: 균형 잡힌 가벼운 부하
        cpu_loop_count = 400000
        dummy_sum = 0.0
        for i in range(cpu_loop_count):
            dummy_sum += i
        
        # 가벼운 메모리 점유 (약 16MB로 축소)
        dummy_memory_holder = [0.456] * 2000000
        time.sleep(0.05)
        time.sleep(0.05)
        return dummy_sum % 1.0


# --- 2. 이종 GPU 시뮬레이터 실행기 (PyTorch Task Runner) ---

class PyTorchTaskRunner:
    """
    실제 PyTorch 연산을 돌리면서 워커 성능 등급에 맞춰 연산 완료 시간을 제어하는 실행기 클래스입니다.

    Attributes:
        task_id (str): 실행할 태스크 고유 ID.
        model_type (str): 신경망 모델 유형 ("CNN" / "RNN" / "LSTM").
        epochs (int): 학습 Epoch 횟수.
        worker_type (str): 워커 유형 ("on_demand" / "spot_a").
        speed_factor (float): 성능 등급별 속도 계수.
        progress (float): 작업 진행률 (0.0 ~ 100.0 %).
        status (str): 작업 실행 상태 ("RUNNING" / "SUCCESS" / "FAILED").
        logs (list): 연산 진행 로그 목록.
        execution_time (float): 총 소요 수행 시간.
    """
    def __init__(self, task_id, model_type, epochs, worker_type, dataset_path=""):
        """
        PyTorchTaskRunner 인스턴스를 초기화합니다.
        """
        self.task_id = task_id
        self.model_type = model_type
        self.epochs = epochs
        self.worker_type = worker_type
        self.dataset_path = dataset_path
        self.task = get_task_by_type(model_type)
        
        type_factors = {
            "on_demand": 1.0,
            "spot_a": 0.6,
            "spot_b": 0.3
        }
        
        self.speed_factor = type_factors.get(worker_type.lower(), 1.0)
        self.progress = 0.0
        self.status = "RUNNING"
        self.logs = []
        self.execution_time = 0.0

    def _check_oom_trigger(self):
        """OOM 예외 유입 조건 감지 및 가상 실패 처리"""
        is_oom_trigger = False
        if self.model_type.upper() == "LSTM" and random.random() < 0.08:
            is_oom_trigger = True
        elif "fail" in self.task_id.lower():
            is_oom_trigger = True
            
        if is_oom_trigger:
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
        print(f"[Worker Task] 가중치 FedAvg 병합 연산 수행: {self.task_id}")
        try:
            paths_str = self.dataset_path.split("merge:")[1]
            file_paths = [p.strip() for p in paths_str.split(",") if p.strip()]
            
            valid_paths = [p for p in file_paths if os.path.exists(p)]
            if not valid_paths:
                raise FileNotFoundError(f"[FedAvg Error] 병합할 유효한 가중치 파일(.pt)이 디바이스상에 하나도 존재하지 않습니다. (요청 리스트: {file_paths})")
            
            print(f"[Worker Task] 유효 파일 스캔 완료: {len(valid_paths)}/{len(file_paths)} 대 병합 진행")
            state_dicts = [torch.load(p, map_location="cpu") for p in valid_paths]
            averaged_sd = {}
            
            base_keys = state_dicts[0].keys()
            for key in base_keys:
                tensors = []
                for sd in state_dicts:
                    if key in sd:
                        tensors.append(sd[key])
                
                if not tensors:
                    continue
                    
                if tensors[0].dtype in [torch.float16, torch.float32, torch.float64, torch.bfloat16]:
                    averaged_sd[key] = torch.stack(tensors).mean(dim=0)
                else:
                    averaged_sd[key] = tensors[0]
            
            os.makedirs("data", exist_ok=True)
            output_path = f"data/final_{self.task_id}.pt"
            torch.save(averaged_sd, output_path)
            
            # [FedAvg 교차 추론 검증]
            inferred_type = "CNN"
            # State dict의 텐서 키값 패턴 매칭을 통해 실제 모델 타입을 정확하게 판별합니다.
            if averaged_sd:
                if any(k.startswith("rnn.") for k in averaged_sd.keys()):
                    inferred_type = "RNN"
                elif any(k.startswith("lstm.") for k in averaged_sd.keys()):
                    inferred_type = "LSTM"
                elif "rnn" in self.task_id.lower():
                    inferred_type = "RNN"
                elif "lstm" in self.task_id.lower():
                    inferred_type = "LSTM"
            
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
                    print(f"[Worker Task] PyTorch 학습 연산 중 경고 발생 (Fallback 대체): {e}")
                    loss = run_dummy_epoch(self.model_type)
            else:
                loss = run_dummy_epoch(self.model_type)

            if HAS_TORCH and device == "cuda":
                torch.cuda.synchronize()

            actual_time = time.time() - epoch_start
            target_time = actual_time / self.speed_factor
            delay = target_time - actual_time
            if delay > 0:
                time.sleep(delay)

            epoch_total_time = actual_time + max(0.0, delay)
            log_line = f"Epoch {epoch+1}/{self.epochs} - Loss: {loss:.4f} - 연산시간: {actual_time:.4f}초 (지연: {max(0.0, delay):.4f}초, 총 {epoch_total_time:.2f}초)"
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
        지정된 AI 모델 학습 연산(또는 모사 연산)을 수행합니다.
        가상 OOM 장애 유발 시나리오 및 성능 차별화 지연(Sleep)을 시뮬레이션합니다.
        """
        print(f"\n[Worker Task] 작업 시작: {self.task_id} (모델: {self.model_type}, 노드타입: {self.worker_type}, 속도배수: {self.speed_factor})")
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
        
        # 5. 학습 루프 수행
        self._run_epoch_loop(model, optimizer, criterion, device)
        
        # 6. 최종 모델 저장 및 추론 검증
        self._save_final_model_and_inference(model, device)
        
        self.execution_time = time.time() - start_time
        if self.status != "FAILED":
            self.status = "SUCCESS"
            
        # 메모리 시뮬레이션 공간 해제
        global dummy_memory_holder
        dummy_memory_holder = []
        
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
