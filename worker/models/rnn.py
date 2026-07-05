import os
import torch
import torch.nn as nn
from worker.models.base import BaseTask

# RNN 학습 하이퍼파라미터
SEQ_BATCH_SIZE = 4
SEQ_NUM_BATCHES = 3
SEQ_LENGTH = 10
SEQ_FEATURE_SIZE = 1
RNN_HIDDEN_SIZE = 16

class RNNModel(nn.Module):
    """2-Layer Stacked RNN 기반 1차원 시계열 예측 신경망 (RNN)"""
    def __init__(self):
        super().__init__()
        self.rnn = nn.RNN(
            input_size=SEQ_FEATURE_SIZE,
            hidden_size=RNN_HIDDEN_SIZE,
            num_layers=2,
            batch_first=True,
            dropout=0.3
        )
        self.fc = nn.Linear(RNN_HIDDEN_SIZE, 1)

    def forward(self, x):
        out, _ = self.rnn(x)
        return self.fc(out[:, -1, :])

class RNNTask(BaseTask):
    """RNN 시계열 예측 학습 및 추론 Task 클래스"""
    def __init__(self):
        super().__init__()

    def get_model(self):
        return RNNModel()

    def get_criterion(self):
        # 수치 연속 예측이므로 MSELoss 적용
        return nn.MSELoss()

    def train_epoch(self, model, optimizer, criterion, device):
        loss_val = 0.0
        for step in range(SEQ_NUM_BATCHES):
            start_x = step * 0.5
            x = torch.linspace(start_x, start_x + 5.0, SEQ_BATCH_SIZE * (SEQ_LENGTH + 1), device=device).view(SEQ_BATCH_SIZE, SEQ_LENGTH + 1)
            y = torch.sin(x)
            
            inputs = y[:, :-1].unsqueeze(-1)
            targets = y[:, -1].unsqueeze(-1)
            
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, targets)
            loss.backward()
            optimizer.step()
            loss_val = loss.item()
            
        return loss_val

    def infer(self, model, device):
        model.eval()
        try:
            with torch.no_grad():
                test_in = torch.sin(torch.linspace(0, 3, SEQ_LENGTH)).view(1, SEQ_LENGTH, 1).to(device)
                preds = []
                for _ in range(5):
                    out = model(test_in)
                    preds.append(out.item())
                    test_in = torch.cat([test_in[:, 1:, :], out.unsqueeze(1)], dim=1)
                formatted_preds = ", ".join([f"{p:.3f}" for p in preds])
                return f"[RNN Forecast Done] 다음 5단계 시계열 예측값 -> [{formatted_preds}]"
        except Exception as e:
            return f"[RNN Inference Error] {str(e)}"
