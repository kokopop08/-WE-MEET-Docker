# ==============================================================================
# WE-MEET: 호스트 물리 자원 관리 및 모니터링 안전 가드 모듈 (head/resource_guard.py)
# ==============================================================================

import os
import psutil
import subprocess

def get_gpu_free_memory():
    """
    [Global Host Resource Manager]
    nvidia-smi 명령어를 호출하여 호스트 GPU의 가용 VRAM 용량(MiB)을 획득합니다.

    Returns:
        int: 가용 GPU VRAM 용량 (MiB 단위, GPU 드라이버 미인식 시 -1 반환).
    """
    try:
        # nvidia-smi --query-gpu=memory.free --format=csv,nounits,noheader
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,nounits,noheader"],
            capture_output=True, text=True, check=True
        )
        free_vram = int(result.stdout.strip())
        return free_vram
    except Exception:
        # GPU 드라이버 미인식 시 모니터링 불가 상태로 간주 (-1 반환)
        return -1

def is_host_resource_sufficient():
    """
    [Global Host Resource Manager]
    호스트 시스템의 실시간 물리 메모리 사용률(%)의 임계 상한선(75% - 16GB 기준)을 검증하여 과부하 방지 안전 여부를 판정합니다.
 
    Returns:
        bool: 호스트 물리 메모리 사용률이 75.0% 이하인 경우 True, 초과한 경우 False.
    """
    if os.environ.get("BYPASS_RESOURCE_GUARD", "0") == "1":
        return True
 
    # [Safety Guard 임계값 75.0% 상한선 선정 이유]
    # RAM 전체 리소스 16GB 기준 75%를 소모할 시 가용 램 여유는 4.0GB가 됩니다.
    # 사용자의 4GB 이상 안전 여유 공간 상한선 제약을 준수하고 버벅임 및 VM 다운을 방지하기 위해 75%로 고정했습니다.
    try:
        mem = psutil.virtual_memory()
        usage_percent = mem.percent
        
        # WSL2 환경 검사 보정 (WSL2에서 메모리 한계를 잡은 경우 free 결과 보조 참고)
        if os.name != 'nt':
            try:
                result = subprocess.run(["free", "-b"], capture_output=True, text=True, timeout=2)
                lines = result.stdout.strip().splitlines()
                for line in lines:
                    if line.startswith("Mem:"):
                        parts = line.split()
                        if len(parts) >= 7:
                            total = int(parts[1])
                            available = int(parts[6])
                            wsl_usage = ((total - available) / total) * 100.0
                            usage_percent = max(usage_percent, wsl_usage)
            except Exception:
                pass
 
        if usage_percent > 75.0:
            print(f"[Global Resource Guard] 호스트 물리 메모리 사용률 상한선 초과 경고: {usage_percent:.1f}% > 75.0% (Safety Guard - Assumed 16GB)")
            return False
        return True
    except Exception as e:
        print(f"[Global Resource Guard] 자원 점검 중 예외 발생: {e}")
        return True
 
def get_recommended_max_spot_scale():
    """
    16GB RAM 시스템을 상정하여 안전하게 띄울 수 있는 최대 스팟 스케일(MAX_SPOT_SCALE) 값을 반환합니다.
    - 16GB 시스템 기준: 5대 허용
    """
    return 5
