# ==============================================================================
# WE-MEET: Q-Learning 오프라인 사전 학습 진입점 (head/q_learning/pretrain.py)
#
# [주의] 과거 이 파일은 4-튜플 상태 + 무작위 전이 휴리스틱으로 학습했으나, 이는 실제 docker 세계와
#        상태 인코딩·물리가 달라 학습된 Q-테이블이 추론에서 통째로 버려지는 문제가 있었다.
#        이제는 실제 세계를 미러링한 단일 시뮬레이터(scratch/train_simulation.py)로 위임한다.
# ==============================================================================

import os
import sys

# 프로젝트 루트를 path에 추가
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from scratch.train_simulation import train_offline


def run_offline_pretraining(episodes=50000):
    """실제 docker 물리를 미러링한 시뮬레이터로 Q-Learning 에이전트를 사전 훈련한다."""
    cost_model_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../common/cost_model.yaml'))
    train_offline(episodes=episodes, cost_model_path=cost_model_path)


if __name__ == "__main__":
    run_offline_pretraining()
