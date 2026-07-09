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
#   └ 확률의 유일 진실(Single Source of Truth)은 common/cost_model.yaml의 preemption_probability.
#   └ Spot-A: 위험구간 50% / 평시 12.5%  (cost_model.yaml 기준)
#   └ Spot-B: 위험구간 10% / 평시 2.5%
#   └ 위험구간(P_spot)은 30초 사이클 중 앞 10초. current_danger_phase()로 판정하며
#     회수 데몬과 스케줄러가 동일 함수를 공유해 값 불일치를 원천 차단한다.
#
# - OutOfCapacity         : Spot 신규 증설 요청 시 클라우드 공급 부족으로 거절
#   └ Spot 노드 신규 기동 시 30% 확률로 거절
# ==============================================================================

import random
import time


class FailureSimulator:
    """
    장애 시뮬레이션 판단 로직 모음 클래스.
    
    모든 메서드는 staticmethod로 구현되어 있어 인스턴스 생성 없이 호출 가능합니다.
    확률 상수를 이 클래스에서 중앙 관리하여 수치 변경 시 한 곳만 수정하면 됩니다.
    """

    # --- OOM (Out-of-Memory) 시뮬레이션 설정 ---
    OOM_PROB_LSTM = 0.08            # (레거시) node_type 미지정 호출 시 LSTM 자연 OOM 확률 (8%)
    OOM_TRIGGER_KEYWORDS = ["fail"] # task_id에 이 문자열이 포함되면 100% OOM 강제 발동 (테스트용)

    # --- 자원경합 기반 OOM 모델 (호스트 부하를 물리적으로 키우지 않고 확률로 모사) ---
    # 노드 메모리 용량(on_demand 2048 / spot_a 1024 / spot_b 512MB) 대비 메모리바운드 모형의
    # 적합도를 확률로 표현한다. 자원을 안 보고 작은 노드에 큰 모형을 얹는 스케줄러가 처벌받는다.
    OOM_BASE_PROB = {
        "LSTM": {"on_demand": 0.02, "spot_a": 0.08, "spot_b": 0.20},  # 메모리 폭식형
        "RNN":  {"on_demand": 0.00, "spot_a": 0.01, "spot_b": 0.03},  # 중간
        # CNN/MERGE 등 연산바운드는 OOM 위험 없음 (0.0)
    }
    OOM_COLOCATION_STEP = 0.08     # 같은 노드에 동거 중인 메모리바운드 태스크 1개당 가산되는 압력
    OOM_PROB_CAP = 0.60            # OOM 확률 상한

    # --- Eviction (Preemption) 시뮬레이션 설정 ---
    # 유일 진실은 cost_model.yaml의 preemption_probability. 아래 값은 yaml 로드 실패 시의 폴백일 뿐이며
    # cost_model.yaml(spot_a 0.50 / spot_b 0.10)과 동일하게 맞춰 표시값-실제값 불일치를 방지한다.
    EVICTION_BASE_PROB = {
        "spot_a": 0.50, # Spot-A: 위험구간(P_spot=1) 시 50% (cost_model.yaml 폴백, 0.30→0.50 상향)
        "spot_b": 0.10, # Spot-B: 위험구간(P_spot=1) 시 10% (cost_model.yaml 폴백)
    }
    EVICTION_IDLE_FACTOR = 0.25     # 평시(P_spot=0)에는 위험구간 확률의 25%로 감소

    # --- OutOfCapacity 시뮬레이션 설정 ---
    OUT_OF_CAPACITY_PROB = 0.30     # Spot 신규 증설 요청 시 공급 부족 거절 확률 (30%)

    # -------------------------------------------------------------------------

    @staticmethod
    def check_oom(model_type: str, task_id: str, node_type: str = None, co_membound_count: int = 0) -> bool:
        """
        해당 태스크가 OOM 장애를 발동해야 하는지 판단합니다.

        node_type이 주어지면(head 측 호출) 노드 메모리 용량 + 동거 압력 기반의 자원경합 모델을 사용하고,
        None이면(레거시 워커측 호출) 기존 LSTM 8% 동작을 그대로 유지합니다.

        Args:
            model_type (str): 실행 중인 모델 유형 ("CNN" / "RNN" / "LSTM").
            task_id (str): 실행 중인 태스크 고유 ID.
            node_type (str, optional): 배치 노드 유형 ("on_demand"/"spot_a"/"spot_b"). None이면 레거시 모드.
            co_membound_count (int): 같은 노드에서 동시에 도는 메모리바운드(RNN/LSTM) 태스크 수.

        Returns:
            bool: True이면 OOM 발동 (태스크를 FAILED 처리해야 함), False이면 정상 진행.
        """
        m = model_type.upper()

        # 조건 A: task_id 강제 트리거 키워드 (테스트/데모용, 100% 발동)
        for keyword in FailureSimulator.OOM_TRIGGER_KEYWORDS:
            if keyword in task_id.lower():
                return True

        # 조건 B(레거시): node_type 미지정 시 기존 LSTM 8% 자연 OOM 유지
        if node_type is None:
            if m == "LSTM" and random.random() < FailureSimulator.OOM_PROB_LSTM:
                return True
            return False

        # 조건 C: 노드 용량 인지 + 동거 압력 기반 자원경합 OOM 확률
        base = FailureSimulator.OOM_BASE_PROB.get(m, {}).get(node_type, 0.0)
        if base <= 0.0 and co_membound_count <= 0:
            return False
        prob = base + FailureSimulator.OOM_COLOCATION_STEP * max(0, co_membound_count)
        prob = min(prob, FailureSimulator.OOM_PROB_CAP)
        return random.random() < prob

    @staticmethod
    def current_danger_phase(now: float = None) -> int:
        """
        현재가 스팟 회수 위험구간(P_spot)인지 판정합니다. 30초 사이클 중 앞 10초가 위험구간(1).

        시간에만 의존하는 결정적 함수이므로, 회수 데몬(cluster_manager)과 스케줄러(q_learning)가
        이 함수를 함께 호출하면 동일한 값을 관측하게 되어 상태 불일치가 발생하지 않습니다.

        Args:
            now (float, optional): 판정 기준 시각(초). None이면 time.time() 사용.

        Returns:
            int: 위험구간이면 1, 평시면 0.
        """
        t = now if now is not None else time.time()
        return 1 if (t % 30.0) < 10.0 else 0

    @staticmethod
    def eviction_probability(node_type: str, p_spot: int, external_probs: dict = None) -> float:
        """
        이번 심사에서 실제로 적용될 회수 확률을 반환합니다 (판정과 로깅의 공용 단일 진실).

        Args:
            node_type (str): 워커 노드 유형 ("spot_a" / "spot_b").
            p_spot (int): 위험구간 여부(1=위험구간 확률 고정, 0=평시 감소).
            external_probs (dict, optional): cost_model.yaml에서 로드한 외부 확률 오버라이드.

        Returns:
            float: 적용 회수 확률(0.0~1.0).
        """
        base_probs = external_probs if external_probs else FailureSimulator.EVICTION_BASE_PROB
        base_prob = base_probs.get(node_type, 0.3)  # 알 수 없는 노드 타입은 30% 폴백
        # 위험구간(p_spot=1): 기본 확률 그대로 / 평시(p_spot=0): 기본 확률 × 25%로 감소
        return base_prob if p_spot == 1 else (base_prob * FailureSimulator.EVICTION_IDLE_FACTOR)

    @staticmethod
    def check_eviction(node_type: str, p_spot: int, external_probs: dict = None) -> bool:
        """
        Eviction Daemon이 해당 Spot 워커를 이번 심사에서 강제 회수해야 하는지 판단합니다.
        적용 확률 계산은 eviction_probability()에 위임하여 로그 표시값과 실제 판정값을 일치시킵니다.

        Args:
            node_type (str): 워커 노드 유형 ("spot_a" / "spot_b").
            p_spot (int): 현재 위험구간 여부. 1이면 위험구간(확률 고정), 0이면 평시(확률 감소).
            external_probs (dict, optional): cost_model.yaml에서 로드한 외부 확률 오버라이드.
                                             None이면 클래스 기본값(EVICTION_BASE_PROB)을 사용.

        Returns:
            bool: True이면 Eviction 발동 (컨테이너를 강제 회수해야 함), False이면 생존.
        """
        eviction_prob = FailureSimulator.eviction_probability(node_type, p_spot, external_probs)
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
