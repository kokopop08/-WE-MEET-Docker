import os
import sys

# PyTorch 임포트 시도
try:
    import torch
    import torch.nn as nn
    HAS_TORCH = True
except ImportError:
    HAS_TORCH = False

class BaseTask:
    """
    모든 PyTorch 학습 및 추론 태스크가 상속받아 구현할 추상 베이스 클래스입니다.
    """
    def __init__(self):
        pass

    def get_model(self):
        """해당 태스크에 매핑된 PyTorch nn.Module 인스턴스를 반환합니다."""
        raise NotImplementedError

    def get_criterion(self):
        """해당 태스크에 적합한 손실(Loss) 함수를 반환합니다."""
        raise NotImplementedError

    def train_epoch(self, model, optimizer, criterion, device):
        """1에포크 분량의 학습 연산을 수행하고 손실 값을 반환합니다."""
        raise NotImplementedError

    def infer(self, model, device):
        """모델 가중치를 활용해 실제 추론을 돌려보고 정량적인 결과 텍스트를 반환합니다."""
        raise NotImplementedError
