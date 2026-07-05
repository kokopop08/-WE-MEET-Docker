from worker.models.base import BaseTask, HAS_TORCH
from worker.models.cnn import CNNTask, CNNModel
from worker.models.rnn import RNNTask, RNNModel
from worker.models.lstm import LSTMTask, LSTMModel

def get_task_by_type(model_type):
    """모델 타입 문자열을 전달받아 대응하는 Task 인스턴스를 반환합니다."""
    upper_type = model_type.upper()
    if upper_type == "CNN":
        return CNNTask()
    elif upper_type == "RNN":
        return RNNTask()
    elif upper_type == "LSTM":
        return LSTMTask()
    return None
