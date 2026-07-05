import os
import torch
import torch.nn as nn
from worker.models.base import BaseTask

# LSTM NLP 학습 하이퍼파라미터
SEQ_BATCH_SIZE = 4
SEQ_NUM_BATCHES = 3
LSTM_VOCAB = ["I", "love", "distributed", "systems", "with", "babyray", "autoscale", "agent"]
LSTM_VOCAB_SIZE = len(LSTM_VOCAB)
LSTM_EMBED_DIM = 16
LSTM_HIDDEN_SIZE = 32

class LSTMModel(nn.Module):
    """2-Layer Stacked LSTM 기반 단어 토큰 문장 생성 신경망 (LSTM)"""
    def __init__(self):
        super().__init__()
        self.embedding = nn.Embedding(LSTM_VOCAB_SIZE, LSTM_EMBED_DIM)
        self.lstm = nn.LSTM(
            input_size=LSTM_EMBED_DIM,
            hidden_size=LSTM_HIDDEN_SIZE,
            num_layers=2,
            batch_first=True,
            dropout=0.3
        )
        self.fc = nn.Linear(LSTM_HIDDEN_SIZE, LSTM_VOCAB_SIZE)

    def forward(self, x):
        x = self.embedding(x)
        out, _ = self.lstm(x)
        return self.fc(out)

class LSTMTask(BaseTask):
    """LSTM NLP 텍스트 문장 생성 학습 및 추론 Task 클래스"""
    def __init__(self):
        super().__init__()

    def get_model(self):
        return LSTMModel()

    def get_criterion(self):
        return nn.CrossEntropyLoss()

    def train_epoch(self, model, optimizer, criterion, device):
        loss_val = 0.0
        for _ in range(SEQ_NUM_BATCHES):
            inputs = torch.tensor([[0, 1, 2, 3, 4, 5, 6]], dtype=torch.long, device=device).repeat(SEQ_BATCH_SIZE, 1)
            targets = torch.tensor([[1, 2, 3, 4, 5, 6, 7]], dtype=torch.long, device=device).repeat(SEQ_BATCH_SIZE, 1)
            
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs.view(-1, LSTM_VOCAB_SIZE), targets.view(-1))
            loss.backward()
            optimizer.step()
            loss_val = loss.item()
            
        return loss_val

    def infer(self, model, device):
        model.eval()
        try:
            with torch.no_grad():
                current_tokens = [0]
                for _ in range(5):
                    inp_tensor = torch.tensor([current_tokens], dtype=torch.long, device=device)
                    out = model(inp_tensor)
                    next_token = torch.argmax(out[0, -1, :]).item()
                    current_tokens.append(next_token)
                generated_sentence = " ".join([LSTM_VOCAB[t] if t < len(LSTM_VOCAB) else "unk" for t in current_tokens])
                return f"[LSTM Generation Done] 텍스트 생성 결과 -> \"{generated_sentence}\""
        except Exception as e:
            return f"[LSTM Inference Error] {str(e)}"
