# ==============================================================================
# WE-MEET: 장애 시뮬레이터 (common/failure_simulator.py)
#
# OOM, Eviction(Preemption), OutOfCapacity 세 가지 장애 시뮬레이션 로직을 중앙화합니다.
# 확률값을 한 곳에서 관리하여 가독성과 유지보수성을 높입니다.
#
# [장애 유형 요약]
# - OOM (Out-of-Memory)   : 워커가 학습 도중 메모리 초과로 태스크를 FAIL 처리
#   └ LSTM 태스크에서 8% 확률로 자연 발생 / task_id에 "fail" 포함 시 강제 발동
#
# - Eviction (Preemption) : 클라우드 플랫폼이 Spot 워커 컨테이너를 강제 회수
#   └ Spot-A: 위험구간 50% / 평시 12.5%
#   └ Spot-B: 위험구간 10% / 평시  2.5%
#
# - OutOfCapacity         : Spot 신규 증설 요청 시 클라우드 공급 부족으로 거절
#   └ Spot 노드 신규 기동 시 30% 확률로 거절
# ==============================================================================

import random


class FailureSimulator:
    """
    장애 시뮬레이션 판단 로직 모음 클래스.
    
    모든 메서드는 staticmethod로 구현되어 있어 인스턴스 생성 없이 호출 가능합니다.
    확률 상수를 이 클래스에서 중앙 관리하여 수치 변경 시 한 곳만 수정하면 됩니다.
    """

    # --- OOM (Out-of-Memory) 시뮬레이션 설정 ---
    OOM_PROB_LSTM = 0.08            # LSTM 태스크에서 자연 발생하는 OOM 확률 (8%)
    OOM_TRIGGER_KEYWORDS = ["fail"] # task_id에 이 문자열이 포함되면 100% OOM 강제 발동 (테스트용)

    # --- Eviction (Preemption) 시뮬레이션 설정 ---
    # eviction_loop에서 cost_model.yaml로 동적 오버라이드 가능 (기본 폴백값)
    EVICTION_BASE_PROB = {
        "spot_a": 0.50, # Spot-A: 위험구간(P_spot=1) 시 50%
        "spot_b": 0.10, # Spot-B: 위험구간(P_spot=1) 시 10%
    }
    EVICTION_IDLE_FACTOR = 0.25     # 평시(P_spot=0)에는 위험구간 확률의 25%로 감소

    # --- OutOfCapacity 시뮬레이션 설정 ---
    OUT_OF_CAPACITY_PROB = 0.30     # Spot 신규 증설 요청 시 공급 부족 거절 확률 (30%)

    # -------------------------------------------------------------------------

    @staticmethod
    def check_oom(model_type: str, task_id: str) -> bool:
        """
        워커가 OOM 장애를 발동해야 하는지 판단합니다.

        Args:
            model_type (str): 실행 중인 모델 유형 ("CNN" / "RNN" / "LSTM").
            task_id (str): 실행 중인 태스크 고유 ID.

        Returns:
            bool: True이면 OOM 발동 (태스크를 FAILED 처리해야 함), False이면 정상 진행.
        """
        # 조건 1: LSTM 모델이면서 OOM_PROB_LSTM 확률(8%)에 걸릴 때
        if model_type.upper() == "LSTM" and random.random() < FailureSimulator.OOM_PROB_LSTM:
            return True

        # 조건 2: task_id에 강제 트리거 키워드가 포함된 경우 (테스트/데모용, 100% 발동)
        for keyword in FailureSimulator.OOM_TRIGGER_KEYWORDS:
            if keyword in task_id.lower():
                return True

        return False

    @staticmethod
    def check_eviction(node_type: str, p_spot: int, external_probs: dict = None) -> bool:
        """
        Eviction Daemon이 해당 Spot 워커를 이번 심사에서 강제 회수해야 하는지 판단합니다.

        Args:
            node_type (str): 워커 노드 유형 ("spot_a" / "spot_b").
            p_spot (int): 현재 위험구간 여부. 1이면 위험구간(확률 고정), 0이면 평시(확률 감소).
            external_probs (dict, optional): cost_model.yaml에서 로드한 외부 확률 오버라이드.
                                             None이면 클래스 기본값(EVICTION_BASE_PROB)을 사용.

        Returns:
            bool: True이면 Eviction 발동 (컨테이너를 강제 회수해야 함), False이면 생존.
        """
        # 외부 오버라이드(cost_model.yaml)가 있으면 우선 적용, 없으면 클래스 기본값 사용
        base_probs = external_probs if external_probs else FailureSimulator.EVICTION_BASE_PROB
        base_prob = base_probs.get(node_type, 0.3) # 알 수 없는 노드 타입은 30% 폴백

        # 위험구간(p_spot=1): 기본 확률 그대로 / 평시(p_spot=0): 기본 확률 × 25%로 감소
        eviction_prob = base_prob if p_spot == 1 else (base_prob * FailureSimulator.EVICTION_IDLE_FACTOR)

        return random.random() < eviction_prob

    @staticmethod
    def check_out_of_capacity(node_type: str) -> bool:
        """
        Spot 노드 신규 증설 요청 시 클라우드 공급 부족(OutOfCapacity)으로 거절해야 하는지 판단합니다.

        Args:
            node_type (str): 증설 요청 중인 노드 유형. Spot 계열("spot_a", "spot_b")에만 적용됩니다.

        Returns:
            bool: True이면 공급 부족 거절 (컨테이너 생성을 중단해야 함), False이면 정상 진행.
        """
        # On-Demand 노드는 OutOfCapacity 모사 대상이 아님
        if node_type not in ["spot_a", "spot_b"]:
            return False

        return random.random() < FailureSimulator.OUT_OF_CAPACITY_PROB
