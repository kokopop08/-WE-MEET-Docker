"""더미 연산 부하 모사 (wemeet/workload/dummy_load.py).

PyTorch 가 없는 환경이나 실연산 실패 시, 모델 종류별로 차별화된 CPU/메모리 부하를 흉내내
스케줄러·자원 가드가 관측할 실측 유사 신호를 만든다. 실제 학습 실행기(runner)와 분리해
'부하 모사'라는 단일 책임만 갖는다.

주의: ``dummy_memory_holder`` 는 모듈 전역이며 ``run_dummy_epoch`` 이 재바인딩한다. 실행기가
에포크 종료 후 메모리를 놓아주려면 ``dummy_load.dummy_memory_holder = []`` 처럼 이 모듈의
속성을 직접 리셋한다(프로세스 내 단일 홀더 공유가 의도된 설계).
"""

import time

#: Fallback 시 최소 대기 시간(초).
FALLBACK_SLEEP = 0.05

#: 더미 메모리 점유 시뮬레이션용 홀더(모듈 전역, run_dummy_epoch 이 재바인딩).
dummy_memory_holder = []


def run_dummy_epoch(model_type):
    """모델 종류별로 차별화된 CPU/메모리 부하를 1 에포크만큼 모사한다.

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
            dummy_sum += (i * 0.0001) ** 0.5  # 제곱근 연산

        dummy_memory_holder = []  # 메모리는 거의 사용하지 않음
        time.sleep(0.02)
        return dummy_sum % 1.0

    elif model_upper == "LSTM":
        # LSTM: 메모리 점유형 (높은 메모리 사용률 유도)
        # 1GB 컨테이너 기준 약 12% (120MB) 임시 점유로 축소 조정 (OOM 방지)
        # float(8 bytes) * 15,000,000 = 약 120MB
        mem_element_count = 15000000
        dummy_memory_holder = [0.123] * mem_element_count  # 약 120MB 메모리 점유

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
        dummy_memory_holder = [0.456] * 2000000  # 16MB
        time.sleep(0.05)
        time.sleep(0.05)
        return dummy_sum % 1.0
