import os
import torch
import torch.nn as nn
from worker.models.base import BaseTask

# CNN 학습 하이퍼파라미터
CNN_BATCH_SIZE = 10
CNN_NUM_BATCHES = 4
CNN_IMAGE_SIZE = 28
CNN_NUM_CLASSES = 10

def get_inline_mnist_dataset(device='cpu'):
    """
    외부 네트워크 다운로드 없이, 파이썬 파일 임포트 형태로 
    즉시 연산 가능한 0~9 손글씨 모사 28x28 픽셀 패턴 데이터셋을 반환합니다.
    """
    images = []
    labels = []
    
    for digit in range(10):
        # 28x28 픽셀 맵
        img = torch.zeros(1, 28, 28, device=device)
        
        # 각 숫자의 전형적인 뼈대 픽셀을 명시적으로 활성화
        if digit == 0:
            img[0, 5:23, 5] = 1.0
            img[0, 5:23, 22] = 1.0
            img[0, 5, 5:23] = 1.0
            img[0, 22, 5:23] = 1.0
        elif digit == 1:
            img[0, 3:25, 14] = 1.0
        elif digit == 2:
            img[0, 5, 5:23] = 1.0
            img[0, 5:14, 22] = 1.0
            img[0, 14, 5:23] = 1.0
            img[0, 14:23, 5] = 1.0
            img[0, 22, 5:23] = 1.0
        elif digit == 3:
            img[0, 5, 5:23] = 1.0
            img[0, 14, 5:23] = 1.0
            img[0, 22, 5:23] = 1.0
            img[0, 5:23, 22] = 1.0
        elif digit == 4:
            img[0, 5:14, 5] = 1.0
            img[0, 14, 5:23] = 1.0
            img[0, 5:23, 22] = 1.0
        elif digit == 5:
            img[0, 5, 5:23] = 1.0
            img[0, 5:14, 5] = 1.0
            img[0, 14, 5:23] = 1.0
            img[0, 14:23, 22] = 1.0
            img[0, 22, 5:23] = 1.0
        elif digit == 6:
            img[0, 5:23, 5] = 1.0
            img[0, 14, 5:23] = 1.0
            img[0, 22, 5:23] = 1.0
            img[0, 14:23, 22] = 1.0
        elif digit == 7:
            img[0, 5, 5:23] = 1.0
            img[0, 5:23, 22] = 1.0
        elif digit == 8:
            img[0, 5:23, 5] = 1.0
            img[0, 5:23, 22] = 1.0
            img[0, 5, 5:23] = 1.0
            img[0, 14, 5:23] = 1.0
            img[0, 22, 5:23] = 1.0
        elif digit == 9:
            img[0, 5:14, 5] = 1.0
            img[0, 5, 5:23] = 1.0
            img[0, 14, 5:23] = 1.0
            img[0, 5:23, 22] = 1.0
            
        images.append(img)
        labels.append(torch.tensor(digit, dtype=torch.long, device=device))
        
    # 배치 형성을 위해 데이터셋 샘플을 적당히 복제하여 증강 반환
    return torch.stack(images * 4), torch.stack(labels * 4)

class CNNModel(nn.Module):
    """3-Layer Conv + BatchNorm + Dropout 기반 이미지 분류 합성곱 신경망 (CNN)"""
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 32, kernel_size=3, padding=1)
        self.bn1 = nn.BatchNorm2d(32)
        self.conv2 = nn.Conv2d(32, 64, kernel_size=3, padding=1)
        self.bn2 = nn.BatchNorm2d(64)
        self.conv3 = nn.Conv2d(64, 128, kernel_size=3, padding=1)
        self.bn3 = nn.BatchNorm2d(128)
        self.pool = nn.MaxPool2d(2, 2)
        self.dropout = nn.Dropout(0.3)
        self.fc1 = nn.Linear(128 * 3 * 3, 128)
        self.fc2 = nn.Linear(128, CNN_NUM_CLASSES)

    def forward(self, x):
        x = self.pool(torch.relu(self.bn1(self.conv1(x))))
        x = self.pool(torch.relu(self.bn2(self.conv2(x))))
        x = self.pool(torch.relu(self.bn3(self.conv3(x))))
        x = x.view(-1, 128 * 3 * 3)
        x = self.dropout(x)
        x = torch.relu(self.fc1(x))
        return self.fc2(x)

class CNNTask(BaseTask):
    """CNN 이미지 분류 학습 및 추론 Task 클래스"""
    def __init__(self):
        super().__init__()
        self.images = None
        self.targets = None

    def get_model(self):
        return CNNModel()

    def get_criterion(self):
        return nn.CrossEntropyLoss()

    def train_epoch(self, model, optimizer, criterion, device):
        # 인라인으로 구현된 로컬 손글씨 모사 픽셀 데이터셋을 GPU 상에 직접 캐싱/재사용
        if self.images is None or self.images.device != torch.device(device):
            self.images, self.targets = get_inline_mnist_dataset(device)
        
        loss_val = 0.0
        for _ in range(CNN_NUM_BATCHES):
            # 난수 셔플 인덱싱으로 배치 분할
            indices = torch.randperm(self.images.size(0))[:CNN_BATCH_SIZE]
            inputs = self.images[indices]
            batch_targets = self.targets[indices]
            
            optimizer.zero_grad()
            outputs = model(inputs)
            loss = criterion(outputs, batch_targets)
            loss.backward()
            optimizer.step()
            loss_val = loss.item()
            
        return loss_val

    def infer(self, model, device):
        model.eval()
        try:
            with torch.no_grad():
                # 인라인 데이터셋 생성 후 숫자 '3'에 대응하는 패턴 이미지 추출
                if self.images is None or self.images.device != torch.device(device):
                    self.images, self.targets = get_inline_mnist_dataset(device)
                test_img = self.images[3:4] # [1, 1, 28, 28]
                
                out = model(test_img)
                prob = torch.softmax(out, dim=1)
                conf, pred = torch.max(prob, dim=1)
                return f"[CNN Inference Done] 이미지 분석 결과 -> 예측 클래스: {pred.item()} (신뢰도: {conf.item()*100:.2f}%)"
        except Exception as e:
            return f"[CNN Inference Error] {str(e)}"
