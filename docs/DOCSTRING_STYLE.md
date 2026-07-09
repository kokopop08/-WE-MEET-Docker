# WE-MEET Docstring 표준 (Google 스타일)

향후 `pdoc` 로 Java(Javadoc)류의 HTML API 문서를 자동 생성한다. 모든 **모듈·공개 클래스·공개
함수**는 아래 Google 스타일 docstring 규약을 따른다. (밑줄 `_` 로 시작하는 내부 헬퍼는 1줄 요약만 권장.)

## 모듈
파일 최상단(코드보다 먼저) 삼중따옴표 docstring:

```python
"""한 줄 목적 (무엇을 하는 모듈인가) — (파일경로).

필요 시 설계 배경/제약을 문단으로. '유일 진실', 의도적 단순화 등은 여기에 기록한다.
"""
```

> 주의: `# ===` 배너 주석은 pdoc 이 모듈 문서로 인식하지 못한다. 반드시 `"""..."""` 로.

## 함수 / 메서드

```python
def scale_out_worker(node_type):
    """신규 Spot 워커 컨테이너를 동적으로 기동한다.

    Args:
        node_type (str): 가동할 노드 종류 ("spot_a" / "spot_b").

    Returns:
        bool: 성공 시 True, 자원 가드/OutOfCapacity 로 거부 시 False.

    Raises:
        docker.errors.APIError: 컨테이너 런타임 오류 전파 시.
    """
```

- 첫 줄: 명령형 한 문장 요약.
- `Args:` 각 인자 `이름 (타입): 설명`.
- `Returns:` `타입: 설명` (없으면 생략).
- `Raises:` 의도적으로 던지는 예외만.

## 클래스

```python
class QLearningAgent:
    """비용/SLA 인지형 스케줄링 행동을 학습하는 Q-Learning 에이전트.

    Attributes:
        alpha (float): 학습률.
        gamma (float): 할인율.
    """
```

## 생성 방법

```bash
# Windows PowerShell
docs/build.ps1
# bash
bash docs/build.sh
```
결과: `docs/api/` 아래 HTML. 브라우저로 `docs/api/index.html` 열기.
